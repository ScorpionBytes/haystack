from typing import List, Callable, Dict, Any

import os
import logging
import json

import tenacity
import requests
import sseclient

from haystack.preview.lazy_imports import LazyImport
from haystack.preview.components.generators.openai.errors import (
    OpenAIError,
    OpenAIRateLimitError,
    OpenAIUnauthorizedError,
)


with LazyImport() as tiktoken_import:
    import tiktoken


logger = logging.getLogger(__name__)


OPENAI_TIMEOUT = float(os.environ.get("HAYSTACK_REMOTE_API_TIMEOUT_SEC", 30))
OPENAI_BACKOFF = int(os.environ.get("HAYSTACK_REMOTE_API_BACKOFF_SEC", 10))
OPENAI_MAX_RETRIES = int(os.environ.get("HAYSTACK_REMOTE_API_MAX_RETRIES", 5))
OPENAI_TOKENIZERS = {
    **tiktoken.model.MODEL_TO_ENCODING,
    "gpt-35-turbo": "cl100k_base",  # https://github.com/openai/tiktoken/pull/72
}
OPENAI_TOKENIZERS_TOKEN_LIMITS = {
    "gpt2": 2049,  # Ref: https://platform.openai.com/docs/models/gpt-3
    "text-davinci": 4097,  # Ref: https://platform.openai.com/docs/models/gpt-3
    "gpt-35-turbo": 2049,  # Ref: https://platform.openai.com/docs/models/gpt-3-5
    "gpt-3.5-turbo": 2049,  # Ref: https://platform.openai.com/docs/models/gpt-3-5
    "gpt-3.5-turbo-16k": 16384,  # Ref: https://platform.openai.com/docs/models/gpt-3-5
    "gpt-3": 4096,  # Ref: https://platform.openai.com/docs/models/gpt-3
    "gpt-4-32k": 32768,  # Ref: https://platform.openai.com/docs/models/gpt-4
    "gpt-4": 8192,  # Ref: https://platform.openai.com/docs/models/gpt-4
}


#: Retry on OpenAI errors
openai_retry = tenacity.retry(
    reraise=True,
    retry=tenacity.retry_if_exception_type(OpenAIError)
    and tenacity.retry_if_not_exception_type(OpenAIUnauthorizedError),
    wait=tenacity.wait_exponential(multiplier=OPENAI_BACKOFF),
    stop=tenacity.stop_after_attempt(OPENAI_MAX_RETRIES),
)


def default_streaming_callback(token: str):
    """
    Default callback function for streaming responses from OpenAI API.
    Prints the tokens to stdout as soon as they are received and returns them.
    """
    print(token, flush=True, end="")
    return token


@openai_retry
def query_chat_model(url: str, headers: Dict[str, str], payload: Dict[str, Any]) -> List[str]:
    """
    Query ChatGPT without streaming the response.

    :param url: The URL to query.
    :param headers: The headers to send with the request.
    :param payload: The payload to send with the request.
    :return: A list of strings containing the response from the OpenAI API.
    """
    response = requests.post(url, headers=headers, data=json.dumps(payload), timeout=OPENAI_TIMEOUT)
    raise_for_status(response=response)
    json_response = json.loads(response.text)
    check_truncated_answers(result=json_response, payload=payload)
    check_filtered_answers(result=json_response, payload=payload)
    return [choice["message"]["content"].strip() for choice in json_response["choices"]]


@openai_retry
def query_chat_model_stream(
    url: str, headers: Dict[str, str], payload: Dict[str, Any], callback: Callable, marker: str
) -> List[str]:
    """
    Query ChatGPT and streams the response. Once the stream finishes, returns a list of strings just like
    self._query_llm()

    :param url: The URL to query.
    :param headers: The headers to send with the request.
    :param payload: The payload to send with the request.
    :param callback: A callback function that is called when a new token is received from the stream.
        The callback function should accept two parameters: the token received from the stream and **kwargs.
        The callback function should return the token that will be returned at the end of the streaming.
    :param marker: A marker that indicates the end of the stream. It is used to determine when to stop streaming.
    :return: A list of strings containing the response from the OpenAI API.
    """
    response = requests.post(url, headers=headers, data=json.dumps(payload), timeout=OPENAI_TIMEOUT)
    raise_for_status(response=response)

    client = sseclient.SSEClient(response)
    tokens = []
    try:
        for event in client.events():
            if event.data == marker:
                break
            event_data = json.loads(event.data)
            delta = event_data["choices"][0]["delta"]
            token = delta["content"] if "content" in delta else None
            if token:
                tokens.append(callback(token, event_data=event_data["choices"]))
    finally:
        client.close()
    return ["".join(tokens)]


def raise_for_status(response: requests.Response):
    """
    Raises the appropriate OpenAI error in case of a bad response.

    :param response: The response returned from the OpenAI API.
    :raises OpenAIError: If the response status code is not 200.
    """
    if response.status_code >= 400:
        openai_error: OpenAIError
        if response.status_code == 429:
            openai_error = OpenAIRateLimitError(f"API rate limit exceeded: {response.text}")
        elif response.status_code == 401:
            openai_error = OpenAIUnauthorizedError(f"API key is invalid: {response.text}")
        else:
            openai_error = OpenAIError(
                f"OpenAI returned an error.\n"
                f"Status code: {response.status_code}\n"
                f"Response body: {response.text}",
                status_code=response.status_code,
            )
        raise openai_error


def check_truncated_answers(result: Dict[str, Any], payload: Dict[str, Any]):
    """
    Check the `finish_reason` the answers returned by OpenAI completions endpoint.
    If the `finish_reason` is `length`, log a warning to the user.

    :param result: The result returned from the OpenAI API.
    :param payload: The payload sent to the OpenAI API.
    """
    truncated_completions = sum(1 for ans in result["choices"] if ans["finish_reason"] == "length")
    if truncated_completions > 0:
        logger.warning(
            "%s out of the %s completions have been truncated before reaching a natural stopping point. "
            "Increase the max_tokens parameter to allow for longer completions.",
            truncated_completions,
            payload["n"],
        )


def check_filtered_answers(result: Dict[str, Any], payload: Dict[str, Any]):
    """
    Check the `finish_reason` the answers returned by OpenAI completions endpoint.
    If the `finish_reason` is `content_filter`, log a warning to the user.

    :param result: The result returned from the OpenAI API.
    :param payload: The payload sent to the OpenAI API.
    """
    filtered_completions = sum(1 for ans in result["choices"] if ans["finish_reason"] == "content_filter")
    if filtered_completions > 0:
        logger.warning(
            "%s out of the %s completions have omitted content due to a flag from OpenAI content filters.",
            filtered_completions,
            payload["n"],
        )