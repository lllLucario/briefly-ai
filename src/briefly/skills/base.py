from dataclasses import dataclass
from importlib import resources


def read_prompt(name: str) -> str:
    return (resources.files("briefly") / "prompts" / f"{name}.md").read_text(encoding="utf-8")


@dataclass(frozen=True)
class Skill:
    """一个能力 = 名称 + 描述 + prompts/<name>.md 里的能力说明。"""

    name: str
    description: str

    @property
    def prompt(self) -> str:
        return read_prompt(self.name)
