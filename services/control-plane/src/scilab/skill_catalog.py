from pathlib import Path


_PACK_NAME = "general-research"
_MANIFEST = Path(__file__).resolve().parents[4] / "skills" / "packs" / "general-research.yaml"


def get_skill_pack(pack_id: str) -> list[str]:
    if pack_id != _PACK_NAME:
        raise ValueError(f"unsupported skill pack: {pack_id}")

    lines = _MANIFEST.read_text(encoding="utf-8").splitlines()
    try:
        start = lines.index("skills:") + 1
    except ValueError as exc:
        raise RuntimeError("general-research manifest has no skills section") from exc

    skills = []
    for line in lines[start:]:
        if line and not line.startswith(" "):
            break
        if line.startswith("  - name: "):
            skill = line.removeprefix("  - name: ").strip()
            if not skill:
                raise RuntimeError("general-research manifest contains an empty skill name")
            skills.append(skill)

    if not skills:
        raise RuntimeError("general-research manifest contains no skills")
    return skills
