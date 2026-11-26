"""Explicit requests about the location or name of a pictured landmark."""
import re


_LANDMARK_REQUEST = re.compile(
    r"(?:geoint|геоинт|геоінт)"
    r"|(?:what|which) (?:landmark|place) is (?:this|that)(?: in (?:the|this) (?:photo|picture))?"
    r"|(?:identify|recognize|name) (?:this|the) (?:public )?landmark(?: in (?:the|this) (?:photo|picture))?"
    r"|where (?:is (?:this|that)(?: (?:place|landmark))?|was (?:this|the) (?:photo|picture) taken|was this taken)"
    r"|(?:где|де) (?:це|это)(?: (?:знято|снято|сфотографовано))?"
    r"|(?:где|де) (?:сделано|зроблено|снято|знято) (?:(?:это|це) )?фото"
    r"|(?:что|що) (?:это |це )?за (?:место|місце|(?:публичная )?достопримечательность|(?:публічна )?пам['’ʼ]ятка)(?: на (?:этом |цьому )?фото)?"
    r"|(?:какая это достопримечательность|яка це пам['’ʼ]ятка)(?: на фото)?"
    r"|(?:определи|определить|визнач|впізнай|распознай) (?:это |це )?"
    r"(?:место|місце|достопримечательность|пам['’ʼ]ятку)(?: (?:на|по|за|з) (?:этому |цим )?фото)?"
)
_VERIFY_SUFFIX = re.compile(
    r"(?:[.?!,]\s*|\s+(?:і|й|та|и|and)\s+)"
    r"(?:перевір (?:видимі )?підказки|проверь (?:видимые )?подсказки|check (?:the )?(?:visible )?clues)$"
)
_ENGLISH_SUFFIX = re.compile(
    r"[,?.!\s]+(?:(?:answer|reply|respond) in english|in english|(?:відповідай |відповідь )?англійською"
    r"|(?:ответь |отвечай |ответ )?на английском)$"
)


def landmark_request_language(prompt: str) -> str | None:
    if len(prompt) > 220:
        return None
    text = " ".join(prompt.casefold().split()).strip(" .?!")
    english = _ENGLISH_SUFFIX.search(text)
    if english:
        text = text[:english.start()].rstrip(" ,.!?")
    text = _VERIFY_SUFFIX.sub("", text).rstrip(" ,.!?")
    text = re.sub(r"^(?:please|пожалуйста|будь ласка)[, ]+", "", text)
    text = re.sub(r"^(?:geoint|геоинт|геоінт)\s*[:,—-]\s*", "", text)
    if _LANDMARK_REQUEST.fullmatch(text):
        return "en" if english else "uk"
    return None
