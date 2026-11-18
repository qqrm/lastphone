"""The BERT WordPiece profile used by bge-small-en-v1.5, without a Rust wheel.

The portable library supplies tokenizer.json, not executable tokenizer code. Reject
other tokenizer families/configurations instead of silently changing embeddings.
This implementation is checked differentially against Hugging Face tokenizers in
tests/test_tokenizer.py. It is deliberately not a general tokenizer replacement.
"""
import json
import re
import unicodedata
from pathlib import Path
from lastphone_unicode import NONSPACING, PUNCTUATION, is_control, is_whitespace


class WordPieceTokenizer:
    def __init__(self, config, max_length=320):
        if isinstance(config, (str, Path)):
            config = json.loads(Path(config).read_text(encoding="utf-8"))
        self.max_length = int(max_length)
        if not 2 <= self.max_length <= 4096:
            raise ValueError("Unsupported BGE maximum length")
        model = config.get("model") or {}
        normalizer = config.get("normalizer") or {}
        pre = config.get("pre_tokenizer") or {}
        post = config.get("post_processor") or {}
        if model.get("type") != "WordPiece" or pre.get("type") != "BertPreTokenizer":
            raise ValueError("Semantic search requires the BGE BERT WordPiece tokenizer")
        if normalizer.get("type") != "BertNormalizer":
            raise ValueError("Unsupported BGE normalizer")
        if model.get("continuing_subword_prefix", "##") != "##":
            raise ValueError("Unsupported WordPiece continuation prefix")
        self.vocab = model.get("vocab") or {}
        if not self.vocab or any(not isinstance(v, int) or v < 0 for v in self.vocab.values()):
            raise ValueError("Invalid WordPiece vocabulary")
        self.unk = model.get("unk_token", "[UNK]")
        self.limit = int(model.get("max_input_chars_per_word", 100))
        if self.unk not in self.vocab or not 1 <= self.limit <= 10000:
            raise ValueError("Invalid WordPiece unknown token")
        for token in ("[CLS]", "[SEP]", "[PAD]"):
            if token not in self.vocab:
                raise ValueError("Missing BGE special token")
        self.clean = normalizer.get("clean_text", True)
        self.chinese = normalizer.get("handle_chinese_chars", True)
        self.lower = normalizer.get("lowercase", True)
        self.accents = normalizer.get("strip_accents")
        if self.accents is None:
            self.accents = self.lower
        if post.get("type") == "BertProcessing":
            valid = post.get("cls") == ["[CLS]", self.vocab["[CLS]"]] and post.get("sep") == ["[SEP]", self.vocab["[SEP]"]]
        elif post.get("type") == "TemplateProcessing":
            valid = post.get("single") == [
                {"SpecialToken": {"id": "[CLS]", "type_id": 0}},
                {"Sequence": {"id": "A", "type_id": 0}},
                {"SpecialToken": {"id": "[SEP]", "type_id": 0}},
            ]
            specials = post.get("special_tokens") or {}
            valid = valid and all((specials.get(t) or {}).get("ids") == [self.vocab[t]] for t in ("[CLS]", "[SEP]"))
        else:
            valid = False
        if not valid:
            raise ValueError("Unsupported BGE single-sequence postprocessor")
        self.added = {}
        for item in config.get("added_tokens") or []:
            token = item.get("content")
            if (token not in ("[PAD]", "[UNK]", "[CLS]", "[SEP]", "[MASK]")
                    or item.get("id") != self.vocab.get(token) or not item.get("special")
                    or any(item.get(k, False) for k in ("single_word", "lstrip", "rstrip", "normalized"))):
                raise ValueError("Unsupported BGE added token")
            self.added[token] = self.vocab[token]
        self.special_re = re.compile("(" + "|".join(re.escape(t) for t in sorted(self.added, key=len, reverse=True)) + ")") if self.added else None

    @staticmethod
    def _chinese(c):
        n = ord(c)
        return (0x4E00 <= n <= 0x9FFF or 0x3400 <= n <= 0x4DBF or 0x20000 <= n <= 0x2A6DF
                or 0x2A700 <= n <= 0x2B73F or 0x2B740 <= n <= 0x2B81F or 0x2B920 <= n <= 0x2CEAF
                or 0xF900 <= n <= 0xFAFF or 0x2F800 <= n <= 0x2FA1F)

    @staticmethod
    def _punctuation(c):
        n = ord(c)
        return 33 <= n <= 47 or 58 <= n <= 64 or 91 <= n <= 96 or 123 <= n <= 126 or n in PUNCTUATION

    def _normalize(self, text):
        if self.clean:
            out = []
            for c in text:
                if ord(c) in (0, 0xFFFD) or (c not in "\t\n\r" and is_control(c)):
                    continue
                out.append(" " if is_whitespace(c) else c)
            text = "".join(out)
        if self.chinese:
            text = "".join(" " + c + " " if self._chinese(c) else c for c in text)
        if self.accents:
            text = "".join(c for c in unicodedata.normalize("NFD", text) if ord(c) not in NONSPACING)
        if self.lower:
            # Rust lowercases individual chars, not Python's contextual final
            # sigma transformation on a complete string.
            text = "".join(c.lower() for c in text)
        return text

    def _pieces(self, text):
        token = []
        for c in self._normalize(text):
            if is_whitespace(c) or self._punctuation(c):
                if token:
                    yield "".join(token)
                    token = []
                if not is_whitespace(c):
                    yield c
            else:
                token.append(c)
        if token:
            yield "".join(token)

    def _word(self, word):
        if len(word) > self.limit:
            return [self.vocab[self.unk]]
        ids, start = [], 0
        while start < len(word):
            end = len(word)
            while end > start:
                piece = ("##" if start else "") + word[start:end]
                if piece in self.vocab:
                    ids.append(self.vocab[piece])
                    break
                end -= 1
            if end == start:
                return [self.vocab[self.unk]]
            start = end
        return ids

    def encode(self, text):
        ids = []
        for part in self.special_re.split(str(text)) if self.special_re else [str(text)]:
            if part in self.added:
                ids.append(self.added[part])
            else:
                for word in self._pieces(part):
                    ids.extend(self._word(word))
        return [self.vocab["[CLS]"], *ids[:self.max_length - 2], self.vocab["[SEP]"]]

    def encode_batch(self, texts):
        rows = [self.encode(text) for text in texts]
        width = max(map(len, rows), default=0)
        # tokenizers.enable_padding() defaults to id 0, including this BGE profile.
        if self.vocab["[PAD]"] != 0:
            raise ValueError("Unsupported BGE padding id")
        masks = [[1] * len(row) + [0] * (width - len(row)) for row in rows]
        return [row + [0] * (width - len(row)) for row in rows], masks
