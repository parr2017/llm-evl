"""Built-in prompt set, custom prompt file load/save, and prompt resolution.

Built-ins are read-only (seeded in code). Custom prompts live in
`prompts.yaml` and are editable from the UI. get_prompts() merges both,
applying the prompt_ids filter last.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from .models import PromptItem

# Three representative built-in prompts spanning output-length buckets.
# Fixed set guarantees cross-run comparability; the long prompt stabilizes
# tokens/s by amortizing the startup overhead over many tokens.
BUILTIN_PROMPTS: list[PromptItem] = [
    PromptItem(
        id="short",
        label="问候",
        bucket="short",
        text="回复一句友好的问候语,只回一句,不要其它内容。",
    ),
    PromptItem(
        id="medium",
        label="解释递归",
        bucket="medium",
        text="用 3-4 个短段落向编程初学者解释递归。给出至少一个代码示例。",
        expected_keywords=["递归", "函数", "调用"],
        min_output_tokens=80,
        # Points carry the quality signal here; keywords alone can be hit by
        # an answer that is on-topic but wrong.
        reference_points=["调用自己", "终止条件", "代码示例"],
    ),
    PromptItem(
        id="long",
        label="500字短文",
        bucket="long",
        text=(
            "写一篇约 500 字的短文,主题是开源软件对现代软件开发的影响。"
            "包含引言、两个带具体例子的正文段落和结论。"
        ),
        expected_keywords=["开源", "软件", "开发"],
        min_output_tokens=300,
        reference_points=["引言", "具体例子", "结论"],
    ),
]

PROMPTS_FILE = "prompts.yaml"


def load_custom_prompts(path: str = PROMPTS_FILE) -> list[PromptItem]:
    """Load user-defined prompts from prompts.yaml. Returns [] if absent."""
    p = Path(path)
    if not p.exists():
        return []
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    items = raw.get("prompts") or []
    out: list[PromptItem] = []
    for d in items:
        out.append(PromptItem(
            id=d.get("id") or f"custom-{len(out)+1}",
            label=d.get("label", "Custom"),
            text=d.get("text", ""),
            bucket=d.get("bucket", "medium"),
            expected_keywords=list(d.get("expected_keywords") or []),
            min_output_tokens=d.get("min_output_tokens"),
            reference_points=list(d.get("reference_points") or []),
            custom=True,
        ))
    return out


def save_custom_prompts(prompts: list[PromptItem], path: str = PROMPTS_FILE) -> None:
    """Persist custom prompts to prompts.yaml."""
    doc = {"prompts": [
        {
            "id": p.id,
            "label": p.label,
            "text": p.text,
            "bucket": p.bucket,
            "expected_keywords": p.expected_keywords,
            "min_output_tokens": p.min_output_tokens,
            "reference_points": p.reference_points,
        }
        for p in prompts if p.custom
    ]}
    Path(path).write_text(
        yaml.safe_dump(doc, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def get_prompts(prompt_ids: list[str] | None = None,
                prompts_file: str = "",
                custom_prompts: list[PromptItem] | None = None) -> list[PromptItem]:
    """Resolve the active prompt list.

    - custom_prompts (in-memory) takes precedence; else load from prompts_file.
    - built-ins always included.
    - prompt_ids filters/selects by id (applied last).
    """
    items: list[PromptItem] = list(BUILTIN_PROMPTS)
    if custom_prompts is not None:
        items.extend(custom_prompts)
    elif prompts_file:
        items.extend(load_custom_prompts(prompts_file))
    else:
        items.extend(load_custom_prompts())

    if prompt_ids:
        wanted = set(prompt_ids)
        items = [p for p in items if p.id in wanted]
    return items
