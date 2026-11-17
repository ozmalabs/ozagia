# Compression Research Sources

References for the lossless compression techniques used in ozma's
compression pipeline.

## Lossless Token Compression

### Meta-Tokens (LZ77-style subsequence replacement)

**Paper:** "Lossless Token Sequence Compression via Meta-Tokens"
**Authors:** John Harvill, Ziwei Fan, Hao Wang, Luke Huan, Anoop Deoras, Yizhou Sun, Hao Ding
**Link:** https://arxiv.org/abs/2506.00307

Repeated multi-token subsequences replaced with single meta-tokens,
dictionary prepended. 18-27% token reduction, 33-47% compute savings
(quadratic attention). Fully lossless, no fine-tuning needed.

**Our implementation:** `ozma/compress/metatokens.py` — LZ77-inspired
repeated substring detection with word-boundary alignment, greedy
meta-token assignment, code block protection.

### Dictionary Encoding via In-Context Learning

**Paper:** "Lossless Prompt Compression via Dictionary-Encoding and
In-Context Learning: Enabling Cost-Effective LLM Analysis of Repetitive Data"
**Authors:** Andresa Rodrigues de Campos, David Lee, Imry Kissos, Piyush Paritosh
**Link:** https://arxiv.org/abs/2604.13066
**Published:** March 2026

Dictionary in system prompt, model learns encoding in-context. 0.99
exact match rate at 60-80% compression on repetitive data (LogHub 2.0
benchmark, Claude 3.7 Sonnet). No fine-tuning. Works with API-based LLMs.

**Validates:** Our IR symbol table approach (inject legend, model uses
symbols, client expands). Extends to all repeated content, not just
code keywords.

### LoPace (Storage Compression)

**Paper:** "LoPace: A Lossless Optimized Prompt Accurate Compression
Engine for Large Language Model Applications"
**Link:** https://arxiv.org/abs/2602.13266

Zstandard + BPE binary packing. 72.2% space savings, fully lossless.
Storage/caching layer compression (not token compression).

**Relevant to:** Our artifact storage and response caching.

## Lossy Prompt Compression (Reference)

### LLMLingua / LLMLingua-2

**Paper:** "LLMLingua: Compressing Prompts for Accelerated Inference of
Large Language Models"
**Authors:** Huiqiang Jiang, Qianhui Wu, Chin-Yew Lin, Yuqing Yang, Lili Qiu
**Links:**
- LLMLingua: https://arxiv.org/abs/2310.05736 (EMNLP 2023)
- LLMLingua-2: https://llmlingua.com/llmlingua2.html (ACL 2024)
- Site: https://www.llmlingua.com/

Small model (GPT-2 / LLaMA-7B) scores token importance via perplexity,
drops low-value tokens. 2-20x compression, 1.5% quality loss on GSM8K.
LLMLingua-2 is 3-6x faster via distilled classifier.

**Our approach differs:** We use lossless compression (abbreviations,
meta-tokens, symbol tables) instead of lossy token dropping. Our wire
format preserves ALL information.

### CompactPrompt

**Paper:** "CompactPrompt: A Unified Pipeline for Prompt and Data
Compression in LLM Workflows"
**Link:** https://arxiv.org/html/2510.18043v1

Self-information scoring + n-gram abbreviation + phrase grouping.
60% token reduction, <5% accuracy drop on Claude 3.5 Sonnet / GPT-4.1-Mini.

### Selective Context

**Paper:** "Selective Context" (related to LLMLingua family)

Mutual information based token filtering. Removes tokens with low
conditional information.

### Prompt Compression Survey

**Paper:** "Prompt Compression for Large Language Models: A Survey"
**Link:** https://github.com/ZongqianLi/Prompt-Compression-Survey
**Venue:** NAACL 2025 Main (Selected Oral)

Comprehensive survey of all prompt compression methods. Categorizes
into lossy vs lossless, hard vs soft, training-free vs fine-tuned.

## Scale-Aware Brevity (Output Compression)

### Inverse Scaling via Scale-Aware Prompting

**Paper:** "Inverse Scaling Can Be Easily Overcome With Scale-Aware
Prompting"
**Authors:** Hakim et al.
**Link:** arXiv:2604.00025 (2026)

Larger models suffer "spontaneous scale-dependent verbosity." Brevity
constraints improve large-model accuracy by 26 percentage points and
can reverse the scaling hierarchy.

**Our implementation:** `ozma/compress/brevity.py` — ModelTier-aware
brevity constraints, Caveman mode (75% output reduction).

**Superseded by:** `ozma/compress/wire.py` — lossless wire format
that preserves full reasoning while compressing encoding. Brevity
(lossy) is no longer needed when wire compression (lossless) is available.

## Our Compression Stack

| Layer | Type | Method | Savings | Source |
|---|---|---|---|---|
| Ozma IR (code keywords) | Lossless | Symbol table | 36.5% on code | Internal |
| Tree-sitter stubs | Lossless | AST body removal | 68-96% on code | Internal |
| Meta-tokens | Lossless | LZ77-style | 18-27% on prose | arXiv 2506.00307 |
| Wire format | Lossless | Abbreviations + symbols | 20-30% on prose | Internal (informed by research) |
| Log compression | Lossless | Dedup + collapse | 80-97% on logs | Internal |
| Stack trace compression | Lossless | Frame collapse | 39-64% on traces | Internal |
| Diff compression | Lossless | Context strip | 44-56% on diffs | Internal |
| Terminal compression | Lossless | Command-aware | 50-90% on terminal | Internal |
| Cross-message dedup | Lossless | Fingerprint + trigram | Variable | Internal |
| Block parser | Router | Content-type detection | Compounds all above | Internal |
