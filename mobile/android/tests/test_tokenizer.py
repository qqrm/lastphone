import copy
import hashlib
import json
import random
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "app" / "src" / "main" / "python"))
from lastphone_tokenizer import WordPieceTokenizer
from tokenizers import Tokenizer  # Mandatory host-only reference; absence fails CI.

FIXTURE = HERE / "fixtures" / "bge-tokenizer.json"
SHA256 = "d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66"


class TokenizerParity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if hashlib.sha256(FIXTURE.read_bytes()).hexdigest() != SHA256:
            raise AssertionError("The pinned BGE tokenizer fixture changed")
        cls.config = json.loads(FIXTURE.read_text(encoding="utf-8"))
        cls.mobile = WordPieceTokenizer(cls.config)
        cls.reference = Tokenizer.from_file(str(FIXTURE))
        cls.reference.enable_truncation(max_length=320)
        cls.reference.enable_padding()

    def compare(self, texts):
        ids, masks = self.mobile.encode_batch(texts)
        expected = self.reference.encode_batch(texts)
        self.assertEqual(ids, [row.ids for row in expected])
        self.assertEqual(masks, [row.attention_mask for row in expected])

    def test_real_bge_profile_unicode_special_tokens_and_padding(self):
        self.compare(["", "Boil clean water for one minute.", "Café À bientôt!", "İı ÄÖÜ ß ﬁ naïve", "你好，世界。東京",
                      "Привет, мир! Вода и фильтрация.", "\x00\x01\u00ad\ue000\ufffd\u200b\t\nriver",
                      "[CLS] x[MASK] [UNK] [PAD][SEP]", "[mask] != [MASK]", "Áé {one}…—‘two’",
                      "ΟΣ ΟΣΑ Σ σ ς", "\ua9da\ua6fc\u08db\u1734\u2e61", "\U0002b820\U0002b920",
                      "word" * 101, "word " * 600, "emoji 😀🫠🏝️ water", "\U00020000\U0002a700\uf900"])

    def test_deterministic_unicode_corpus_against_rust_reference(self):
        rng = random.Random(101)
        alphabet = list("Abc water 123.#[]\t\n") + ["[MASK]", "[SEP]", "é", "\x00", "é", "İ", "ß", "水", "你", "\u200b"]
        alphabet += [chr(rng.randrange(1, 0xD800)) for _ in range(300)]
        for _ in range(12):
            self.compare(["".join(rng.choice(alphabet) for _ in range(rng.randrange(1, 600))) for _ in range(8)])

    def test_unsupported_model_and_added_token_are_rejected(self):
        config = copy.deepcopy(self.config)
        config["model"]["type"] = "BPE"
        with self.assertRaisesRegex(ValueError, "WordPiece"):
            WordPieceTokenizer(config)
        config = copy.deepcopy(self.config)
        config["added_tokens"][0]["lstrip"] = True
        with self.assertRaisesRegex(ValueError, "added token"):
            WordPieceTokenizer(config)


if __name__ == "__main__":
    unittest.main()
