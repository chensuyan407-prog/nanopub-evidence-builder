"""Writes the labelled evaluation drafts tests/eval/<paper>/draft.json.

Each assertion carries 'expect': clean (should not fail) or the rule(s) a planted defect must trigger.
Sentence ids refer to sentences.jsonl produced by np_init.py on the PDFs listed in tests/PAPERS.md.
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
F = {"assertions": {"kind": "assertions"}, "evidence": {"kind": "evidence"}, "provenance": {"kind": "provenance"}}


def A(id, text, s, p, o, ev, expect, **kw):
    d = {"id": id, "text": text, "subject": s, "predicate": p, "object": o, "evidence": ev}
    d.update(kw)
    d["expect"] = expect
    return d


PASS = {"clean": True}


def BAD(*r):
    return {"clean": False, "rules": list(r)}


T4 = lambda row, val=None: dict({"type": "table_cell", "table": "Table 4", "row": {"method": row}, "column": "top-5 err."}, **({"value": val} if val else {}))

resnet = [
    A("r01", "An ensemble of residual nets achieves 3.57% error on the ImageNet test set.", "ensemble of residual nets", "achieves", "error on the ImageNet test set",
      ["p1-b5-s6"], PASS, qualifiers={"value": "3.57", "unit": "%", "metric": "error", "dataset": "ImageNet test set"}),
    A("r02", "On ImageNet, the 34-layer ResNet is better than the 18-layer ResNet by 2.8%.", "34-layer ResNet", "outperforms", "18-layer ResNet",
      [{"page": 5, "quote": "the 34-layer ResNet is better than the 18-layer ResNet (by 2.8%)"}], PASS, qualifiers={"value": "2.8", "unit": "%", "dataset": "ImageNet"}),
    A("r03", "ResNet-152 has a single-model top-5 error of 4.49% on the ImageNet validation set.", "ResNet-152", "has_value", "top-5 error",
      [T4("ResNet-152", "4.49")], PASS, qualifiers={"value": "4.49", "unit": "%", "metric": "top-5 err.", "dataset": "ImageNet validation set", "condition": "single model"}),
    A("r04", "ResNet-152 has a lower single-model top-5 validation error than ResNet-34 B on ImageNet.", "ResNet-152", "lower_than", "ResNet-34 B",
      [T4("ResNet-152"), T4("ResNet-34 B")], PASS, evidence_type="inferred", reasoning="Table 4 top-5 err.: ResNet-152 4.49 < ResNet-34 B 5.71",
      qualifiers={"metric": "top-5 err.", "dataset": "ImageNet validation set"}),
    A("r05", "The 34-layer plain net still achieving competitive accuracy suggests that the solver works to some extent.", "solver", "has_property", "works to some extent",
      ["p5-b85-s5"], PASS, modality="hedged"),
    A("r06", "Residual networks are easier to optimize and can gain accuracy from considerably increased depth.", "residual networks", "has_property", "easier to optimize",
      ["p1-b5-s4"], BAD("R07")),
    A("r07", "An ensemble of residual nets achieves 3.75% error on the ImageNet test set.", "ensemble of residual nets", "achieves", "error",
      ["p1-b5-s6"], BAD("R09"), qualifiers={"value": "3.75", "unit": "%", "dataset": "ImageNet test set"}),
    A("r08", "Deeper plain networks have higher training error than shallower plain networks.", "deeper plain networks", "higher_than", "shallower plain networks",
      [{"page": 4, "quote": "deeper plain networks always have much higher training error than shallow ones"}], BAD("R04")),
    A("r09", "ResNet-152 has a single-model top-5 error of 3.57% on the ImageNet validation set.", "ResNet-152", "has_value", "top-5 error",
      [T4("ResNet-152", "3.57")], BAD("R04", "R09"), qualifiers={"value": "3.57", "unit": "%", "dataset": "ImageNet validation set", "condition": "single model"}),
    A("r10", "VGG nets are 8 times shallower than 152-layer residual nets.", "VGG nets", "has_property", "8x shallower than 152-layer residual nets",
      ["p1-b5-s5"], BAD("R20")),
    A("r11", "Residual nets always gain accuracy from increased depth.", "residual nets", "improves", "accuracy",
      [{"page": 5, "quote": "we manage to obtain accuracy gains from increased depth"}], BAD("R15")),
]
att = [
    A("t01", "The Transformer (big) achieves 28.4 BLEU on the WMT 2014 English-to-German translation task.", "Transformer (big)", "achieves", "BLEU on WMT 2014 English-to-German",
      ["p8-b17-s1"], PASS, qualifiers={"value": "28.4", "unit": "BLEU", "dataset": "WMT 2014 English-to-German"}),
    A("t02", "The big Transformer achieves a BLEU score of 41.0 on the WMT 2014 English-to-French translation task.", "Transformer (big)", "achieves", "BLEU on WMT 2014 English-to-French",
      ["p8-b18-s1"], PASS, qualifiers={"value": "41.0", "unit": "BLEU", "dataset": "WMT 2014 English-to-French"}),
    A("t03", "Transformer (big) scores 28.4 BLEU on newstest2014 EN-DE (Table 2).", "Transformer (big)", "has_value", "BLEU EN-DE",
      [{"type": "table_cell", "table": "Table 2", "row": {"Model": "Transformer (big)"}, "column": "BLEU/EN-DE", "value": "28.4"}], PASS,
      qualifiers={"value": "28.4", "metric": "BLEU", "dataset": "newstest2014 EN-DE"}),
    A("t04", "RNN sequence-to-sequence models have not been able to attain state-of-the-art results in small-data regimes.", "RNN sequence-to-sequence models", "lacks_property",
      "state-of-the-art results in small-data regimes", ["p9-b19-s3"], PASS, scope="cited_work", cited_reference="[37]", evidence_type="cited", modality="negated"),
    A("t05", "The big Transformer achieves a BLEU score of 41.8 on the WMT 2014 English-to-French translation task.", "Transformer (big)", "achieves", "BLEU on WMT 2014 English-to-French",
      ["p8-b18-s1"], BAD("R09"), qualifiers={"value": "41.8", "unit": "BLEU", "dataset": "WMT 2014 English-to-French"}),
    A("t06", "The Transformer allows for significantly more parallelization and can reach a new state of the art in translation quality.", "Transformer", "enables", "more parallelization",
      ["p2-b5-s2"], BAD("R07")),
    A("t07", "RNN sequence-to-sequence models have not been able to attain state-of-the-art results in small-data regimes.", "RNN sequence-to-sequence models", "lacks_property",
      "state-of-the-art results in small-data regimes", ["p9-b19-s3"], BAD("R20"), modality="negated"),
    A("t08", "RNN sequence-to-sequence models attain state-of-the-art results in small-data regimes.", "RNN sequence-to-sequence models", "has_property",
      "state-of-the-art results in small-data regimes", ["p9-b19-s3"], BAD("R14"), scope="cited_work", cited_reference="[37]", evidence_type="cited"),
    A("t09", "Single-head attention is always 0.9 BLEU worse than the best setting.", "single-head attention", "underperforms", "best setting",
      ["p9-b16-s2"], BAD("R15"), qualifiers={"value": "0.9", "unit": "BLEU"}),
    A("t10", "The Transformer outperforms the BerkeleyParser on English constituency parsing.", "Transformer", "outperforms", "BerkeleyParser",
      ["p10-b5-s1"], BAD("R16")),
    A("t11", "The big Transformer beats the best previously reported models by more than 2 BLEU on WMT 2014 English-to-German.", "Transformer (big)", "outperforms", "best previously reported models",
      [{"page": 8, "quote": "the big transformer beats the best previously reported models by more than 2 BLEU"}], BAD("R04")),
    A("t12", "On WMT 2014 English-to-German the big Transformer establishes a new state-of-the-art BLEU score of 28.4.", "Transformer (big)", "achieves", "state-of-the-art BLEU on WMT 2014 English-to-German",
      [{"page": 3, "quote": "establishing a new state-of-the-art BLEU score of 28.4"}], BAD("R05"), qualifiers={"value": "28.4", "unit": "BLEU", "dataset": "WMT 2014 English-to-German"}),
]
bn = [
    A("b01", "BN-x5 needs 14 times fewer training steps than Inception to reach 72.2% accuracy.", "BN-x5", "outperforms", "Inception",
      ["p7-b12-s3"], PASS, qualifiers={"value": "14", "unit": "x fewer steps", "metric": "training steps to reach 72.2% accuracy"}),
    A("b02", "BN-x5-Sigmoid achieves an accuracy of 69.8%.", "BN-x5-Sigmoid", "achieves", "accuracy", ["p7-b17-s1"], PASS,
      qualifiers={"value": "69.8", "unit": "%", "metric": "accuracy"}),
    A("b03", "Without Batch Normalization, Inception with sigmoid never achieves better than 1/1000 accuracy.", "Inception with sigmoid, without Batch Normalization", "lacks_property",
      "accuracy better than 1/1000", ["p7-b17-s2"], PASS, modality="negated"),
    A("b04", "Inception reached 72.2% accuracy after 31 · 10^6 training steps.", "Inception", "achieves", "accuracy", ["p7-b10-s2"], PASS,
      qualifiers={"value": "72.2", "unit": "%", "condition": "after 31 · 10^6 training steps"}),
    A("b05", "An ensemble of batch-normalized networks reaches 4.9% top-5 validation error on ImageNet classification.", "ensemble of batch-normalized networks", "achieves",
      "top-5 validation error on ImageNet", ["p1-b5-s8"], PASS, qualifiers={"value": "4.9", "unit": "%", "metric": "top-5 validation error", "dataset": "ImageNet"}),
    A("b06", "The batch-normalized ensemble reaches 4.8% top-5 test error.", "batch-normalized ensemble", "achieves", "top-5 test error",
      ["p7-b19-s3"], BAD("R09"), qualifiers={"value": "4.8", "unit": "%"}),
    A("b07", "Batch Normalization causes higher test accuracy.", "Batch Normalization", "causes", "higher test accuracy", ["p6-b4-s2"], BAD("R17")),
    A("b08", "Applying Batch Normalization before the nonlinearity results in a stable distribution of activation values.", "applying Batch Normalization before the nonlinearity",
      "causes", "stable distribution of activation values", ["p8-b5-s2"], BAD("R12")),
    A("b09", "Batch Normalization achieves the same accuracy with 14 times fewer training steps and beats the original model by a significant margin.", "Batch Normalization",
      "improves", "training speed", ["p1-b5-s7"], BAD("R07")),
    A("b10", "Batch Normalization speeds up training of the Inception network.", "Batch Normalization", "speeds_up", "Inception training", ["p7-b12-s2"], BAD("R02")),
    A("b11", "BN-x30 reaches a higher maximum accuracy than BN-x5.", "BN-x30", "higher_than", "BN-x5", ["p7-b15-s1"], BAD("R22"), evidence_type="inferred",
      qualifiers={"metric": "max accuracy"}),
    A("b12", "BN-Inception reports a top-5 error of 4.94%.", "BN-Inception", "has_value", "top-5 error", ["p7-b19-s2"], BAD("R19"), qualifiers={"value": "4.94", "unit": "%"}),
]
CONF = {
    "resnet": [],
    "attention": [{"issue": "abstract (p1-b16-s6) and Table 2 report 41.8 BLEU EN-FR for Transformer (big), Sec. 6.1 text reports 41.0 (p8-b18-s1)", "evidence": ["p1-b16-s6", "p8-b18-s1"]}],
    "bn": [{"issue": "abstract: 4.8% test error (p1-b5-s8) vs Sec. 4.2.3: 4.82% (p7-b19-s3), rounding", "evidence": ["p1-b5-s8", "p7-b19-s3"]}],
}
if __name__ == "__main__":
    n = 0
    for name, lst in (("resnet", resnet), ("attention", att), ("bn", bn)):
        os.makedirs(os.path.join(HERE, "eval", name), exist_ok=True)
        with open(os.path.join(HERE, "eval", name, "draft.json"), "w", encoding="utf-8") as fh:
            json.dump({"source": "paper", "assertions": lst, "fields": F, "conflicts": CONF[name]}, fh, ensure_ascii=False, indent=1)
        n += len(lst)
    print(n, "assertions written")
