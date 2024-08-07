# standard import
import base64
import json
import logging
import mimetypes
from collections.abc import Generator
from typing import Optional, Union, cast

# 3rd import
import boto3
import requests
from botocore.config import Config
from botocore.exceptions import (
    ClientError,
    EndpointConnectionError,
    NoRegionError,
    ServiceNotInRegionError,
    UnknownServiceError,
)
from cohere import ChatMessage

# local import
from core.model_runtime.entities.llm_entities import LLMResult, LLMResultChunk, LLMResultChunkDelta
from core.model_runtime.entities.message_entities import (
    AssistantPromptMessage,
    ImagePromptMessageContent,
    PromptMessage,
    PromptMessageContentType,
    PromptMessageTool,
    SystemPromptMessage,
    TextPromptMessageContent,
    ToolPromptMessage,
    UserPromptMessage,
)
from core.model_runtime.errors.invoke import (
    InvokeAuthorizationError,
    InvokeBadRequestError,
    InvokeConnectionError,
    InvokeError,
    InvokeRateLimitError,
    InvokeServerUnavailableError,
)
from core.model_runtime.errors.validate import CredentialsValidateFailedError
from core.model_runtime.model_providers.__base.large_language_model import LargeLanguageModel
from core.model_runtime.model_providers.cohere.llm.llm import CohereLargeLanguageModel

logger = logging.getLogger(__name__)

class BedrockLargeLanguageModel(LargeLanguageModel):

    def _invoke(self, model: str, credentials: dict,
                prompt_messages: list[PromptMessage], model_parameters: dict,
                tools: Optional[list[PromptMessageTool]] = None, stop: Optional[list[str]] = None,
                stream: bool = True, user: Optional[str] = None) \
            -> Union[LLMResult, Generator]:
        """
        Invoke large language model

        :param model: model name
        :param credentials: model credentials
        :param prompt_messages: prompt messages
        :param model_parameters: model parameters
        :param tools: tools for tool calling
        :param stop: stop words
        :param stream: is stream response
        :param user: unique user id
        :return: full response or stream response chunk generator result
        """
        # TODO: consolidate different invocation methods for models based on base model capabilities
        # invoke anthropic models via boto3 client
        if "anthropic" in model:
            return self._generate_anthropic(model, credentials, prompt_messages, model_parameters, stop, stream, user, tools)
        # invoke Cohere models via boto3 client
        if "cohere.command-r" in model:
            return self._generate_cohere_chat(model, credentials, prompt_messages, model_parameters, stop, stream, user, tools)
        # invoke other models via boto3 client
        return self._generate(model, credentials, prompt_messages, model_parameters, stop, stream, user)
    
    def _generate_cohere_chat(
            self, model: str, credentials: dict, prompt_messages: list[PromptMessage], model_parameters: dict,
            stop: Optional[list[str]] = None, stream: bool = True, user: Optional[str] = None,
            tools: Optional[list[PromptMessageTool]] = None,) -> Union[LLMResult, Generator]:
        cohere_llm = CohereLargeLanguageModel()
        client_config = Config(
            region_name=credentials["aws_region"]
        )

        runtime_client = boto3.client(
            service_name='bedrock-runtime',
            config=client_config,
            aws_access_key_id=credentials["aws_access_key_id"],
            aws_secret_access_key=credentials["aws_secret_access_key"]
        )

        extra_model_kwargs = {}
        if stop:
            extra_model_kwargs['stop_sequences'] = stop

        if tools:
            tools = cohere_llm._convert_tools(tools)
            model_parameters['tools'] = tools

        message, chat_histories, tool_results \
            = cohere_llm._convert_prompt_messages_to_message_and_chat_histories(prompt_messages)

        if tool_results:
            model_parameters['tool_results'] = tool_results

        payload = {
            **model_parameters,
            "message": message,
            "chat_history": chat_histories,
        }

        # need workaround for ai21 models which doesn't support streaming
        if stream:
            invoke = runtime_client.invoke_model_with_response_stream
        else:
            invoke = runtime_client.invoke_model

        def serialize(obj):
            if isinstance(obj, ChatMessage):
                return obj.__dict__
            raise TypeError(f"Type {type(obj)} not serializable")

        try:
            body_jsonstr=json.dumps(payload, default=serialize)
            response = invoke(
                modelId=model,
                contentType="application/json",
                accept="*/*",
                body=body_jsonstr
            )
        except ClientError as ex:
            error_code = ex.response['Error']['Code']
            full_error_msg = f"{error_code}: {ex.response['Error']['Message']}"
            raise self._map_client_to_invoke_error(error_code, full_error_msg)

        except (EndpointConnectionError, NoRegionError, ServiceNotInRegionError) as ex:
            raise InvokeConnectionError(str(ex))

        except UnknownServiceError as ex:
            raise InvokeServerUnavailableError(str(ex))

        except Exception as ex:
            raise InvokeError(str(ex))

        if stream:
            return self._handle_generate_stream_response(model, credentials, response, prompt_messages)

        return self._handle_generate_response(model, credentials, response, prompt_messages)


    def _generate_anthropic(self, model: str, credentials: dict, prompt_messages: list[PromptMessage], model_parameters: dict,
                stop: Optional[list[str]] = None, stream: bool = True, user: Optional[str] = None, tools: Optional[list[PromptMessageTool]] = None,) -> Union[LLMResult, Generator]:
        """
        Invoke Anthropic large language model

        :param model: model name
        :param credentials: model credentials
        :param prompt_messages: prompt messages
        :param model_parameters: model parameters
        :param stop: stop words
        :param stream: is stream response
        :return: full response or stream response chunk generator result
        """
        bedrock_client = boto3.client(service_name='bedrock-runtime',
                                      aws_access_key_id=credentials.get("aws_access_key_id"),
                                      aws_secret_access_key=credentials.get("aws_secret_access_key"),
                                      region_name=credentials["aws_region"])

        system, prompt_message_dicts = self._convert_converse_prompt_messages(prompt_messages)
        inference_config, additional_model_fields = self._convert_converse_api_model_parameters(model_parameters, stop)

        parameters = {
            'modelId': model,
            'messages': prompt_message_dicts,
            'inferenceConfig': inference_config,
            'additionalModelRequestFields': additional_model_fields,
        }

        if system and len(system) > 0:
            parameters['system'] = system

        if tools:
            parameters['toolConfig'] = self._convert_converse_tool_config(tools=tools)

        if stream:
            response = bedrock_client.converse_stream(**parameters)
            return self._handle_converse_stream_response(model, credentials, response, prompt_messages)
        else:
            response = bedrock_client.converse(**parameters)
            return self._handle_converse_response(model, credentials, response, prompt_messages)

    def _handle_converse_response(self, model: str, credentials: dict, response: dict,
                                prompt_messages: list[PromptMessage]) -> LLMResult:
        """
        Handle llm chat response

        :param model: model name
        :param credentials: credentials
        :param response: response
        :param prompt_messages: prompt messages
        :return: full response chunk generator result
        """
        # transform assistant message to prompt message
        assistant_prompt_message = AssistantPromptMessage(
            content=response['output']['message']['content'][0]['text']
        )

        # calculate num tokens
        if response['usage']:
            # transform usage
            prompt_tokens = response['usage']['inputTokens']
            completion_tokens = response['usage']['outputTokens']
        else:
            # calculate num tokens
            prompt_tokens = self.get_num_tokens(model, credentials, prompt_messages)
            completion_tokens = self.get_num_tokens(model, credentials, [assistant_prompt_message])

        # transform usage
        usage = self._calc_response_usage(model, credentials, prompt_tokens, completion_tokens)

        result = LLMResult(
            model=model,
            prompt_messages=prompt_messages,
            message=assistant_prompt_message,
            usage=usage,
        )
        return result

    def _handle_converse_stream_response(self, model: str, credentials: dict, response: dict,
                                        prompt_messages: list[PromptMessage], ) -> Generator:
        """
        Handle llm chat stream response

        :param model: model name
        :param credentials: credentials
        :param response: response
        :param prompt_messages: prompt messages
        :return: full response or stream response chunk generator result
        """

        try:
            full_assistant_content = ''
            return_model = None
            input_tokens = 0
            output_tokens = 0
            finish_reason = None
            index = 0
            tool_calls: list[AssistantPromptMessage.ToolCall] = []
            tool_use = {}

            for chunk in response['stream']:
                if 'messageStart' in chunk:
                    return_model = model
                elif 'messageStop' in chunk:
                    finish_reason = chunk['messageStop']['stopReason']
                elif 'contentBlockStart' in chunk:
                    tool = chunk['contentBlockStart']['start']['toolUse']
                    tool_use['toolUseId'] = tool['toolUseId']
                    tool_use['name'] = tool['name']
                elif 'metadata' in chunk:
                    input_tokens = chunk['metadata']['usage']['inputTokens']
                    output_tokens = chunk['metadata']['usage']['outputTokens']
                    usage = self._calc_response_usage(model, credentials, input_tokens, output_tokens)
                    yield LLMResultChunk(
                        model=return_model,
                        prompt_messages=prompt_messages,
                        delta=LLMResultChunkDelta(
                            index=index,
                            message=AssistantPromptMessage(
                                content='',
                                tool_calls=tool_calls
                            ),
                            finish_reason=finish_reason,
                            usage=usage
                        )
                    )
                elif 'contentBlockDelta' in chunk:
                    delta = chunk['contentBlockDelta']['delta']
                    if 'text' in delta:
                        chunk_text = delta['text'] if delta['text'] else ''
                        full_assistant_content += chunk_text
                        assistant_prompt_message = AssistantPromptMessage(
                            content=chunk_text if chunk_text else '',
                        )
                        index = chunk['contentBlockDelta']['contentBlockIndex']
                        yield LLMResultChunk(
                            model=model,
                            prompt_messages=prompt_messages,
                            delta=LLMResultChunkDelta(
                                index=index+1,
                                message=assistant_prompt_message,
                            )
                        )
                    elif 'toolUse' in delta:
                        if 'input' not in tool_use:
                            tool_use['input'] = ''
                        tool_use['input'] += delta['toolUse']['input']
                elif 'contentBlockStop' in chunk:
                    if 'input' in tool_use:
                        tool_call = AssistantPromptMessage.ToolCall(
                            id=tool_use['toolUseId'],
                            type='function',
                            function=AssistantPromptMessage.ToolCall.ToolCallFunction(
                                name=tool_use['name'],
                                arguments=tool_use['input']
                            )
                        )
                        tool_calls.append(tool_call)
                        tool_use = {}

        except Exception as ex:
            raise InvokeError(str(ex))
    
    def _convert_converse_api_model_parameters(self, model_parameters: dict, stop: Optional[list[str]] = None) -> tuple[dict, dict]:
        inference_config = {}
        additional_model_fields = {}
        if 'max_tokens' in model_parameters:
            inference_config['maxTokens'] = model_parameters['max_tokens']

        if 'temperature' in model_parameters:
            inference_config['temperature'] = model_parameters['temperature']
        
        if 'top_p' in model_parameters:
            inference_config['topP'] = model_parameters['temperature']

        if stop:
            inference_config['stopSequences'] = stop
        
        if 'top_k' in model_parameters:
            additional_model_fields['top_k'] = model_parameters['top_k']
        
        return inference_config, additional_model_fields

    def _convert_converse_prompt_messages(self, prompt_messages: list[PromptMessage]) -> tuple[str, list[dict]]:
        """
        Convert prompt messages to dict list and system
        """

        system = []
        for message in prompt_messages:
            if isinstance(message, SystemPromptMessage):
                message.content=message.content.strip()
                system.append({"text": message.content})

        prompt_message_dicts = []
        for message in prompt_messages:
            if not isinstance(message, SystemPromptMessage):
                prompt_message_dicts.append(self._convert_prompt_message_to_dict(message))

        return system, prompt_message_dicts

    def _convert_converse_tool_config(self, tools: Optional[list[PromptMessageTool]] = None) -> dict:
        tool_config = {}
        configs = []
        if tools:
            for tool in tools:
                configs.append(
                    {
                        "toolSpec": {
                            "name": tool.name,
                            "description": tool.description,
                            "inputSchema": {
                                "json": tool.parameters
                            }
                        }
                    }
                )
            tool_config["tools"] = configs
            return tool_config
    
    def _convert_prompt_message_to_dict(self, message: PromptMessage) -> dict:
        """
        Convert PromptMessage to dict
        """
        if isinstance(message, UserPromptMessage):
            message = cast(UserPromptMessage, message)
            if isinstance(message.content, str):
                message_dict = {"role": "user", "content": [{'text': message.content}]}
            else:
                sub_messages = []
                for message_content in message.content:
                    if message_content.type == PromptMessageContentType.TEXT:
                        message_content = cast(TextPromptMessageContent, message_content)
                        sub_message_dict = {
                            "text": message_content.data
                        }
                        sub_messages.append(sub_message_dict)
                    elif message_content.type == PromptMessageContentType.IMAGE:
                        message_content = cast(ImagePromptMessageContent, message_content)
                        if not message_content.data.startswith("data:"):
                            # fetch image data from url
                            try:
                                image_content = requests.get(message_content.data).content
                                mime_type, _ = mimetypes.guess_type(message_content.data)
                            except Exception as ex:
                                raise ValueError(f"Failed to fetch image data from url {message_content.data}, {ex}")
                        else:
                            data_split = message_content.data.split(";base64,")
                            mime_type = data_split[0].replace("data:", "")
                            base64_data = data_split[1]
                            image_content = base64.b64decode(base64_data)

                        if mime_type not in ["image/jpeg", "image/png", "image/gif", "image/webp"]:
                            raise ValueError(f"Unsupported image type {mime_type}, "
                                             f"only support image/jpeg, image/png, image/gif, and image/webp")

                        sub_message_dict = {
                            "image": {
                                "format": mime_type.replace('image/', ''),
                                "source": {
                                    "bytes": image_content
                                }
                            }
                        }
                        sub_messages.append(sub_message_dict)

                message_dict = {"role": "user", "content": sub_messages}
        elif isinstance(message, AssistantPromptMessage):
            message = cast(AssistantPromptMessage, message)
            if message.tool_calls:
                message_dict = {
                    "role": "assistant", "content":[{
                        "toolUse": {
                            "toolUseId": message.tool_calls[0].id,
                            "name": message.tool_calls[0].function.name,
                            "input": json.loads(message.tool_calls[0].function.arguments)
                        }
                    }]
                }
            else:
                message_dict = {"role": "assistant", "content": [{'text': message.content}]}
        elif isinstance(message, SystemPromptMessage):
            message = cast(SystemPromptMessage, message)
            message_dict = [{'text': message.content}]
        elif isinstance(message, ToolPromptMessage):
            message = cast(ToolPromptMessage, message)
            message_dict = {
                "role": "user",
                "content": [{
                    "toolResult": {
                        "toolUseId": message.tool_call_id,
                        "content": [{"json": {"text": message.content}}]
                    }                   
                }]
            }
        else:
            raise ValueError(f"Got unknown type {message}")

        return message_dict

    def get_num_tokens(self, model: str, credentials: dict, prompt_messages: list[PromptMessage] | str,
                       tools: Optional[list[PromptMessageTool]] = None) -> int:
        """
        Get number of tokens for given prompt messages

        :param model: model name
        :param credentials: model credentials
        :param prompt_messages: prompt messages or message string
        :param tools: tools for tool calling
        :return:md = genai.GenerativeModel(model)
        """
        prefix = model.split('.')[0]
        model_name = model.split('.')[1]
        
        if isinstance(prompt_messages, str):
            prompt = prompt_messages
        else:
            prompt = self._convert_messages_to_prompt(prompt_messages, prefix, model_name)


        return self._get_num_tokens_by_gpt2(prompt)

    def validate_credentials(self, model: str, credentials: dict) -> None:
        """
        Validate model credentials

        :param model: model name
        :param credentials: model credentials
        :return:
        """
        required_params = {}
        if "anthropic" in model:
            required_params = {
                "max_tokens": 32,
            }
        elif "ai21" in model:
            # ValidationException: Malformed input request: #/temperature: expected type: Number, found: Null#/maxTokens: expected type: Integer, found: Null#/topP: expected type: Number, found: Null, please reformat your input and try again.
            required_params = {
                "temperature": 0.7,
                "topP": 0.9,
                "maxTokens": 32,
            }
            
        try:
            ping_message = UserPromptMessage(content="ping")
            self._invoke(model=model,
                           credentials=credentials,
                           prompt_messages=[ping_message],
                           model_parameters=required_params,
                           stream=False)
        
        except ClientError as ex:
            error_code = ex.response['Error']['Code']
            full_error_msg = f"{error_code}: {ex.response['Error']['Message']}"

            raise CredentialsValidateFailedError(str(self._map_client_to_invoke_error(error_code, full_error_msg)))

        except Exception as ex:
            raise CredentialsValidateFailedError(str(ex))

    def _convert_one_message_to_text(self, message: PromptMessage, model_prefix: str, model_name: Optional[str] = None) -> str:
        """
        Convert a single message to a string.

        :param message: PromptMessage to convert.
        :return: String representation of the message.
        """
        
        if model_prefix == "anthropic":
            human_prompt_prefix = "\n\nHuman:"
            human_prompt_postfix = ""
            ai_prompt = "\n\nAssistant:"

        elif model_prefix == "meta":
            # LLAMA3
            if model_name.startswith("llama3"):
                human_prompt_prefix = "<|eot_id|><|start_header_id|>user<|end_header_id|>\n\n"
                human_prompt_postfix = "<|eot_id|><|start_header_id|>assistant<|end_header_id|>\n\n"
                ai_prompt = "\n\nAssistant:"
            else:
                # LLAMA2
                human_prompt_prefix = "\n[INST]"
                human_prompt_postfix = "[\\INST]\n"
                ai_prompt = ""

        elif model_prefix == "mistral":
            human_prompt_prefix = "<s>[INST]"
            human_prompt_postfix = "[\\INST]\n"
            ai_prompt = "\n\nAssistant:"

        elif model_prefix == "amazon":
            human_prompt_prefix = "\n\nUser:"
            human_prompt_postfix = ""
            ai_prompt = "\n\nBot:"
        
        else:
            human_prompt_prefix = ""
            human_prompt_postfix = ""
            ai_prompt = ""

        content = message.content

        if isinstance(message, UserPromptMessage):
            message_text = f"{human_prompt_prefix} {content} {human_prompt_postfix}"
        elif isinstance(message, AssistantPromptMessage):
            message_text = f"{ai_prompt} {content}"
        elif isinstance(message, SystemPromptMessage):
            message_text = content
        elif isinstance(message, ToolPromptMessage):
            message_text = f"{human_prompt_prefix} {message.content}"
        else:
            raise ValueError(f"Got unknown type {message}")

        return message_text

    def _convert_messages_to_prompt(self, messages: list[PromptMessage], model_prefix: str, model_name: Optional[str] = None) -> str:
        """
        Format a list of messages into a full prompt for the Anthropic, Amazon and Llama models

        :param messages: List of PromptMessage to combine.
        :param model_name: specific model name.Optional,just to distinguish llama2 and llama3
        :return: Combined string with necessary human_prompt and ai_prompt tags.
        """
        if not messages:
            return ''

        messages = messages.copy()  # don't mutate the original list
        if not isinstance(messages[-1], AssistantPromptMessage):
            messages.append(AssistantPromptMessage(content=""))

        text = "".join(
            self._convert_one_message_to_text(message, model_prefix, model_name)
            for message in messages
        )

        # trim off the trailing ' ' that might come from the "Assistant: "
        return text.rstrip()

    def _create_payload(self, model: str, prompt_messages: list[PromptMessage], model_parameters: dict, stop: Optional[list[str]] = None, stream: bool = True):
        """
        Create payload for bedrock api call depending on model provider
        """
        payload = {}
        model_prefix = model.split('.')[0]
        model_name = model.split('.')[1]

        if model_prefix == "amazon":
            payload["textGenerationConfig"] = { **model_parameters }
            payload["textGenerationConfig"]["stopSequences"] = ["User:"]
            
            payload["inputText"] = self._convert_messages_to_prompt(prompt_messages, model_prefix)
        
        elif model_prefix == "ai21":
            payload["temperature"] = model_parameters.get("temperature")
            payload["topP"] = model_parameters.get("topP")
            payload["maxTokens"] = model_parameters.get("maxTokens")
            payload["prompt"] = self._convert_messages_to_prompt(prompt_messages, model_prefix)

            if model_parameters.get("presencePenalty"):
                payload["presencePenalty"] = {model_parameters.get("presencePenalty")}
            if model_parameters.get("frequencyPenalty"):
                payload["frequencyPenalty"] = {model_parameters.get("frequencyPenalty")}
            if model_parameters.get("countPenalty"):
                payload["countPenalty"] = {model_parameters.get("countPenalty")}
        
        elif model_prefix == "mistral":
            payload["temperature"] = model_parameters.get("temperature")
            payload["top_p"] = model_parameters.get("top_p")
            payload["max_tokens"] = model_parameters.get("max_tokens")
            payload["prompt"] = self._convert_messages_to_prompt(prompt_messages, model_prefix)
            payload["stop"] = stop[:10] if stop else []

        elif model_prefix == "anthropic":
            payload = { **model_parameters }
            payload["prompt"] = self._convert_messages_to_prompt(prompt_messages, model_prefix)
            payload["stop_sequences"] = ["\n\nHuman:"] + (stop if stop else [])
            
        elif model_prefix == "cohere":
            payload = { **model_parameters }
            payload["prompt"] = prompt_messages[0].content
            payload["stream"] = stream
        
        elif model_prefix == "meta":
            payload = { **model_parameters }
            payload["prompt"] = self._convert_messages_to_prompt(prompt_messages, model_prefix, model_name)

        else:
            raise ValueError(f"Got unknown model prefix {model_prefix}")
        
        return payload

    def _generate(self, model: str, credentials: dict,
                  prompt_messages: list[PromptMessage], model_parameters: dict,
                  stop: Optional[list[str]] = None, stream: bool = True,
                  user: Optional[str] = None) -> Union[LLMResult, Generator]:
        """
        Invoke large language model

        :param model: model name
        :param credentials: credentials kwargs
        :param prompt_messages: prompt messages
        :param model_parameters: model parameters
        :param stop: stop words
        :param stream: is stream response
        :param user: unique user id
        :return: full response or stream response chunk generator result
        """
        client_config = Config(
            region_name=credentials["aws_region"]
        )

        runtime_client = boto3.client(
            service_name='bedrock-runtime',
            config=client_config,
            aws_access_key_id=credentials.get("aws_access_key_id"),
            aws_secret_access_key=credentials.get("aws_secret_access_key")
        )

        model_prefix = model.split('.')[0]
        payload = self._create_payload(model, prompt_messages, model_parameters, stop, stream)

        # need workaround for ai21 models which doesn't support streaming
        if stream and model_prefix != "ai21":
            invoke = runtime_client.invoke_model_with_response_stream
        else:
            invoke = runtime_client.invoke_model

        try:
            body_jsonstr=json.dumps(payload)
            response = invoke(
                modelId=model,
                contentType="application/json",
                accept= "*/*",
                body=body_jsonstr
            )
        except ClientError as ex:
            error_code = ex.response['Error']['Code']
            full_error_msg = f"{error_code}: {ex.response['Error']['Message']}"
            raise self._map_client_to_invoke_error(error_code, full_error_msg)
        
        except (EndpointConnectionError, NoRegionError, ServiceNotInRegionError) as ex:
            raise InvokeConnectionError(str(ex))

        except UnknownServiceError as ex:
            raise InvokeServerUnavailableError(str(ex))

        except Exception as ex:
            raise InvokeError(str(ex))
        

        if stream:
            return self._handle_generate_stream_response(model, credentials, response, prompt_messages)

        return self._handle_generate_response(model, credentials, response, prompt_messages)

    def _handle_generate_response(self, model: str, credentials: dict, response: dict,
                                  prompt_messages: list[PromptMessage]) -> LLMResult:
        """
        Handle llm response

        :param model: model name
        :param credentials: credentials
        :param response: response
        :param prompt_messages: prompt messages
        :return: llm response
        """
        response_body = json.loads(response.get('body').read().decode('utf-8'))

        finish_reason = response_body.get("error")

        if finish_reason is not None:
            raise InvokeError(finish_reason)

        # get output text and calculate num tokens based on model / provider
        model_prefix = model.split('.')[0]

        if model_prefix == "amazon":
            output = response_body.get("results")[0].get("outputText").strip('\n')
            prompt_tokens = response_body.get("inputTextTokenCount")
            completion_tokens = response_body.get("results")[0].get("tokenCount")

        elif model_prefix == "ai21":
            output = response_body.get('completions')[0].get('data').get('text')
            prompt_tokens = len(response_body.get("prompt").get("tokens"))
            completion_tokens = len(response_body.get('completions')[0].get('data').get('tokens'))

        elif model_prefix == "anthropic":
            output = response_body.get("completion")
            prompt_tokens = self.get_num_tokens(model, credentials, prompt_messages)
            completion_tokens = self.get_num_tokens(model, credentials, output if output else '')
            
        elif model_prefix == "cohere":
            output = response_body.get("generations")[0].get("text")
            prompt_tokens = self.get_num_tokens(model, credentials, prompt_messages)
            completion_tokens = self.get_num_tokens(model, credentials, output if output else '')
            
        elif model_prefix == "meta":
            output = response_body.get("generation").strip('\n')
            prompt_tokens = response_body.get("prompt_token_count")
            completion_tokens = response_body.get("generation_token_count")
        
        elif model_prefix == "mistral":
            output = response_body.get("outputs")[0].get("text")
            prompt_tokens = response.get('ResponseMetadata').get('HTTPHeaders').get('x-amzn-bedrock-input-token-count')
            completion_tokens = response.get('ResponseMetadata').get('HTTPHeaders').get('x-amzn-bedrock-output-token-count')

        else:
            raise ValueError(f"Got unknown model prefix {model_prefix} when handling block response")

        # construct assistant message from output
        assistant_prompt_message = AssistantPromptMessage(
            content=output
        )

        # calculate usage
        usage = self._calc_response_usage(model, credentials, prompt_tokens, completion_tokens)

        # construct response
        result = LLMResult(
            model=model,
            prompt_messages=prompt_messages,
            message=assistant_prompt_message,
            usage=usage,
        )

        return result

    def _handle_generate_stream_response(self, model: str, credentials: dict, response: dict,
                                         prompt_messages: list[PromptMessage]) -> Generator:
        """
        Handle llm stream response

        :param model: model name
        :param credentials: credentials
        :param response: response
        :param prompt_messages: prompt messages
        :return: llm response chunk generator result
        """
        model_prefix = model.split('.')[0]
        if model_prefix == "ai21":
            response_body = json.loads(response.get('body').read().decode('utf-8'))

            content = response_body.get('completions')[0].get('data').get('text')
            finish_reason = response_body.get('completions')[0].get('finish_reason')

            prompt_tokens = len(response_body.get("prompt").get("tokens"))
            completion_tokens = len(response_body.get('completions')[0].get('data').get('tokens'))
            usage = self._calc_response_usage(model, credentials, prompt_tokens, completion_tokens)
            yield LLMResultChunk(
                    model=model,
                    prompt_messages=prompt_messages,
                    delta=LLMResultChunkDelta(
                        index=0,
                        message=AssistantPromptMessage(content=content),
                        finish_reason=finish_reason,
                        usage=usage
                    )
                )
            return
        
        stream = response.get('body')
        if not stream:
            raise InvokeError('No response body')
        
        index = -1
        for event in stream:
            chunk = event.get('chunk')
            
            if not chunk:
                exception_name = next(iter(event))
                full_ex_msg = f"{exception_name}: {event[exception_name]['message']}"
                raise self._map_client_to_invoke_error(exception_name, full_ex_msg)

            payload = json.loads(chunk.get('bytes').decode())

            model_prefix = model.split('.')[0]
            if model_prefix == "amazon":
                content_delta = payload.get("outputText").strip('\n')
                finish_reason = payload.get("completion_reason")
 
            elif model_prefix == "anthropic":
                content_delta = payload.get("completion")
                finish_reason = payload.get("stop_reason")

            elif model_prefix == "cohere":
                content_delta = payload.get("text")
                finish_reason = payload.get("finish_reason")
            
            elif model_prefix == "mistral":
                content_delta = payload.get('outputs')[0].get("text")
                finish_reason = payload.get('outputs')[0].get("stop_reason")

            elif model_prefix == "meta":
                content_delta = payload.get("generation").strip('\n')
                finish_reason = payload.get("stop_reason")
            
            else:
                raise ValueError(f"Got unknown model prefix {model_prefix} when handling stream response")

            # transform assistant message to prompt message
            assistant_prompt_message = AssistantPromptMessage(
                content = content_delta if content_delta else '',
            )
            index += 1
           
            if not finish_reason:
                yield LLMResultChunk(
                    model=model,
                    prompt_messages=prompt_messages,
                    delta=LLMResultChunkDelta(
                        index=index,
                        message=assistant_prompt_message
                    )
                )

            else:
                # get num tokens from metrics in last chunk
                prompt_tokens = payload["amazon-bedrock-invocationMetrics"]["inputTokenCount"]
                completion_tokens = payload["amazon-bedrock-invocationMetrics"]["outputTokenCount"]

                # transform usage
                usage = self._calc_response_usage(model, credentials, prompt_tokens, completion_tokens)
                
                yield LLMResultChunk(
                    model=model,
                    prompt_messages=prompt_messages,
                    delta=LLMResultChunkDelta(
                        index=index,
                        message=assistant_prompt_message,
                        finish_reason=finish_reason,
                        usage=usage
                    )
                )
    
    @property
    def _invoke_error_mapping(self) -> dict[type[InvokeError], list[type[Exception]]]:
        """
        Map model invoke error to unified error
        The key is the ermd = genai.GenerativeModel(model)ror type thrown to the caller
        The value is the md = genai.GenerativeModel(model)error type thrown by the model,
        which needs to be converted into a unified error type for the caller.

        :return: Invoke emd = genai.GenerativeModel(model)rror mapping
        """
        return {
            InvokeConnectionError: [],
            InvokeServerUnavailableError: [],
            InvokeRateLimitError: [],
            InvokeAuthorizationError: [],
            InvokeBadRequestError: []
        }
    
    def _map_client_to_invoke_error(self, error_code: str, error_msg: str) -> type[InvokeError]:
        """
        Map client error to invoke error

        :param error_code: error code
        :param error_msg: error message
        :return: invoke error
        """

        if error_code == "AccessDeniedException":
            return InvokeAuthorizationError(error_msg)
        elif error_code in ["ResourceNotFoundException", "ValidationException"]:
            return InvokeBadRequestError(error_msg)
        elif error_code in ["ThrottlingException", "ServiceQuotaExceededException"]:
            return InvokeRateLimitError(error_msg)
        elif error_code in ["ModelTimeoutException", "ModelErrorException", "InternalServerException", "ModelNotReadyException"]:
            return InvokeServerUnavailableError(error_msg)
        elif error_code == "ModelStreamErrorException":
            return InvokeConnectionError(error_msg)

        return InvokeError(error_msg)
