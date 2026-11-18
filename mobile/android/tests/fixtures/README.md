# BGE tokenizer differential fixture

`bge-tokenizer.json` is an unmodified tokenizer from
[BAAI/bge-small-en-v1.5 at revision 5c38ec7c405ec4b44b94cc5a9bb96e735b38267a](https://huggingface.co/BAAI/bge-small-en-v1.5/blob/5c38ec7c405ec4b44b94cc5a9bb96e735b38267a/tokenizer.json).

SHA-256: `d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66`.

BAAI publishes the model under MIT; the tokenizer uses the BERT vocabulary.
This fixture is only used by host differential tests and is not included in the
APK. Android reads the tokenizer accompanying the user's installed BGE model.
The host test dependency `tokenizers==0.21.4` is the independent reference, not an
Android runtime dependency. Tests fail if the reference or fixture is missing.
