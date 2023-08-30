import logging


logger = logging.getLogger(__name__)


def enforce_token_limit(prompt: str, tokenizer, max_tokens_limit: int) -> str:
    """
    Ensure that the length of the prompt is within the max tokens limit of the model.
    If needed, truncate the prompt text so that it fits within the limit.

    :param prompt: Prompt text to be sent to the generative model.
    :param tokenizer: The tokenizer used to encode the prompt.
    :param max_tokens_limit: The max tokens limit of the model.
    :return: The prompt text that fits within the max tokens limit of the model.
    """
    tokens = tokenizer.encode(prompt)
    tokens_count = len(tokens)
    if tokens_count > max_tokens_limit:
        logger.warning(
            "The prompt has been truncated from %s tokens to %s tokens so that the prompt fits within the max token "
            "limit. Reduce the length of the prompt to prevent it from being cut off.",
            tokens_count,
            max_tokens_limit,
        )
        tokenized_payload = tokenizer.encode(prompt)
        prompt = tokenizer.decode(tokenized_payload[:max_tokens_limit])
    return prompt