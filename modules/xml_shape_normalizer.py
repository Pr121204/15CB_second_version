from __future__ import annotations

import functools
import io
import zipfile
import xml.etree.ElementTree as ET
from collections import Counter
from typing import Dict, List, Literal, Optional, Tuple, TypedDict


DEFAULT_SAMPLE_ZIP_PATH = ""
DECLARATION = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
PARENTS_FOR_PROFILE = (
    "RemitteeAddrs",
    "RemittanceDetails",
    "ItActDetails",
    "DTAADetails",
    "TDSDetails",
)


class ReferenceShape(TypedDict):
    name: str
    root: ET.Element
    mode: Literal["TDS", "NON_TDS"]
    profile: Dict[str, Tuple[str, ...]]
    xml_text: str


class ShapeDiff(TypedDict):
    ok: bool
    mismatches: List[Dict[str, object]]
    counts: Dict[str, int]


def _local(tag: str) -> str:
    if "}" in tag:
        return tag.split("}", 1)[1]
    if ":" in tag:
        return tag.split(":", 1)[1]
    return tag


def _first_line(text: str) -> str:
    return (text.splitlines() or [""])[0].strip()


def _mode_from_root(root: ET.Element) -> Literal["TDS", "NON_TDS"]:
    tags = {_local(n.tag) for n in root.iter()}
    return "TDS" if "RateTdsSecbFlg" in tags else "NON_TDS"


def _find_first_by_local(root: ET.Element, local: str) -> Optional[ET.Element]:
    for node in root.iter():
        if _local(node.tag) == local:
            return node
    return None


def _text_of(root: ET.Element, local: str) -> str:
    node = _find_first_by_local(root, local)
    return (node.text or "").strip() if node is not None else ""


def _child_sequence(root: ET.Element, parent_local: str) -> Tuple[str, ...]:
    node = _find_first_by_local(root, parent_local)
    if node is None:
        return tuple()
    return tuple(_local(c.tag) for c in list(node))


def _profile(root: ET.Element) -> Dict[str, Tuple[str, ...]]:
    return {p: _child_sequence(root, p) for p in PARENTS_FOR_PROFILE}


def _profile_distance(a: Dict[str, Tuple[str, ...]], b: Dict[str, Tuple[str, ...]]) -> int:
    distance = 0
    for parent in PARENTS_FOR_PROFILE:
        ax = a.get(parent, tuple())
        bx = b.get(parent, tuple())
        m = min(len(ax), len(bx))
        distance += abs(len(ax) - len(bx))
        for i in range(m):
            if ax[i] != bx[i]:
                distance += 1
    return distance


def _load_sample_xml_texts(sample_zip_path: str) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    with zipfile.ZipFile(sample_zip_path, "r") as zf:
        for name in zf.namelist():
            if not name.lower().endswith(".xml"):
                continue
            text = zf.read(name).decode("utf8", errors="replace")
            out.append((name, text))
    return out


@functools.lru_cache(maxsize=4)
def load_reference_shapes(sample_zip_path: str = DEFAULT_SAMPLE_ZIP_PATH) -> List[ReferenceShape]:
    shapes: List[ReferenceShape] = []
    for name, text in _load_sample_xml_texts(sample_zip_path):
        try:
            root = ET.fromstring(text)
        except Exception:
            continue
        shapes.append(
            {
                "name": name,
                "root": root,
                "mode": _mode_from_root(root),
                "profile": _profile(root),
                "xml_text": text,
            }
        )
    return shapes


def select_reference_shape(
    generated_xml: str,
    sample_zip_path: str = DEFAULT_SAMPLE_ZIP_PATH,
) -> ReferenceShape:
    shapes = load_reference_shapes(sample_zip_path)
    if not shapes:
        raise ValueError(f"No sample XMLs found at: {sample_zip_path}")

    try:
        generated_root = ET.fromstring(generated_xml)
    except Exception as exc:
        raise ValueError(f"Generated XML is not parseable: {exc}") from exc

    mode = _mode_from_root(generated_root)
    rem_char = _text_of(generated_root, "RemittanceCharIndia")
    generated_profile = _profile(generated_root)

    mode_candidates = [s for s in shapes if s["mode"] == mode] or shapes
    if rem_char:
        same_char = [s for s in mode_candidates if _text_of(s["root"], "RemittanceCharIndia") == rem_char]
        if same_char:
            mode_candidates = same_char

    best = min(mode_candidates, key=lambda s: _profile_distance(generated_profile, s["profile"]))
    return best


def _build_tag_index(children: List[ET.Element]) -> Dict[str, List[ET.Element]]:
    idx: Dict[str, List[ET.Element]] = {}
    for child in children:
        idx.setdefault(child.tag, []).append(child)
    return idx


def _consume(index: Dict[str, List[ET.Element]], tag: str) -> Optional[ET.Element]:
    items = index.get(tag)
    if not items:
        return None
    return items.pop(0)


def _clone_to_shape(reference_node: ET.Element, source_node: Optional[ET.Element]) -> ET.Element:
    out = ET.Element(reference_node.tag, reference_node.attrib)
    ref_children = list(reference_node)
    src_children = list(source_node) if source_node is not None else []

    if not ref_children:
        out.text = (source_node.text if source_node is not None and source_node.text is not None else "")
        return out

    src_index = _build_tag_index(src_children)
    for ref_child in ref_children:
        src_child = _consume(src_index, ref_child.tag)
        out.append(_clone_to_shape(ref_child, src_child))
    return out


def _register_namespaces(reference_root: ET.Element) -> None:
    if reference_root.tag.startswith("{"):
        root_ns = reference_root.tag[1:].split("}", 1)[0]
        ET.register_namespace("FORM15CB", root_ns)
    for node in reference_root.iter():
        if node.tag.startswith("{"):
            ns = node.tag[1:].split("}", 1)[0]
            local = _local(node.tag)
            if local in {"CreationInfo", "Form_Details", "SWVersionNo", "SWCreatedBy", "XMLCreatedBy", "XMLCreationDate", "IntermediaryCity", "FormName", "Description", "AssessmentYear", "SchemaVer", "FormVer", "State"}:
                ET.register_namespace("Form", ns)
                break


def normalize_xml_to_reference_shape(generated_xml: str, reference_xml: str) -> str:
    generated_root = ET.fromstring(generated_xml)
    reference_root = ET.fromstring(reference_xml)
    _register_namespaces(reference_root)
    normalized_root = _clone_to_shape(reference_root, generated_root)
    body = ET.tostring(normalized_root, encoding="unicode")
    return DECLARATION + "\n" + body


def strict_shape_compare(xml_a: str, xml_b: str) -> ShapeDiff:
    root_a = ET.fromstring(xml_a)
    root_b = ET.fromstring(xml_b)
    mismatches: List[Dict[str, object]] = []

    def walk(a: ET.Element, b: ET.Element, path: str) -> None:
        cur = f"{path}/{_local(a.tag)}" if path else f"/{_local(a.tag)}"
        if a.tag != b.tag:
            mismatches.append(
                {"type": "tag_mismatch", "path": cur, "expected": _local(a.tag), "actual": _local(b.tag)}
            )
            return
        ac = list(a)
        bc = list(b)
        if len(ac) != len(bc):
            mismatches.append(
                {"type": "child_count", "path": cur, "expected": len(ac), "actual": len(bc)}
            )
        m = min(len(ac), len(bc))
        exp_seq = [_local(x.tag) for x in ac]
        got_seq = [_local(x.tag) for x in bc]
        if exp_seq[:m] != got_seq[:m]:
            mismatches.append(
                {"type": "child_seq", "path": cur, "expected": exp_seq, "actual": got_seq}
            )
        for i in range(m):
            walk(ac[i], bc[i], cur)

    walk(root_a, root_b, "")
    counts = dict(Counter(str(m.get("type", "")) for m in mismatches))
    return {"ok": not mismatches, "mismatches": mismatches, "counts": counts}
