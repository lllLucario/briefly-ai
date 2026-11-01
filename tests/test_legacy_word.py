from io import BytesIO
from pathlib import Path
import subprocess

import pytest
from docx import Document
from fastapi.testclient import TestClient

from briefly import config, legacy_word
from briefly.api import create_app
from briefly.db import create_db_engine
from briefly.loaders import file_type, load_document
from briefly.materials import MaterialStore


def docx_bytes():
    doc=Document()
    doc.add_heading("虚构住宿制度",level=1)
    doc.add_paragraph("上海住宿上限650元，须事前审批。")
    table=doc.add_table(rows=2,cols=2)
    table.cell(0,0).text="城市"
    table.cell(0,1).text="额度"
    table.cell(1,0).text="杭州"
    table.cell(1,1).text="480元"
    output=BytesIO()
    doc.save(output)
    return output.getvalue()


@pytest.fixture
def converted(monkeypatch,tmp_path):
    monkeypatch.setattr(config,"DOC_PARSER","convert")
    executable=tmp_path/"中文 程序"/"soffice.com"
    executable.parent.mkdir()
    executable.write_bytes(b"fake")
    monkeypatch.setattr(config,"LIBREOFFICE_PATH",str(executable))
    commands=[]
    def run(command, **_):
        commands.append(command)
        source=Path(command[-1])
        assert source.read_bytes().startswith(legacy_word.OLE_HEADER)
        profile=next(c.split("=",1)[1] for c in command if c.startswith("-env:UserInstallation="))
        assert profile.startswith("file:///")
        destination=Path(command[command.index("--outdir")+1])/"source.docx"
        destination.write_bytes(docx_bytes())
    monkeypatch.setattr(legacy_word,"_run",run)
    return commands


def test_convert_then_preserve_headings_tables_and_original_type(converted):
    doc=load_document("中文目录/制度.DOC",legacy_word.OLE_HEADER+b"fixture")
    assert file_type("制度.DOC")=="doc"
    assert "# 虚构住宿制度" in doc.text
    assert "650元" in doc.text and "| 杭州 | 480元 |" in doc.text
    assert doc.markdown is True
    assert doc.report["original_format"]=="doc" and doc.report["warnings"]
    assert not Path(converted[0][-1]).exists()  # 临时文件已清理


def test_missing_runtime_and_misconfigured_path(monkeypatch):
    monkeypatch.setattr(config,"DOC_PARSER","convert")
    monkeypatch.setattr(config,"LIBREOFFICE_PATH","")
    monkeypatch.setattr(legacy_word.shutil,"which",lambda _:None)
    monkeypatch.setattr(Path,"is_file",lambda _:False)
    with pytest.raises(ValueError,match="需要服务端安装"):
        load_document("a.doc",legacy_word.OLE_HEADER+b"fixture")
    monkeypatch.setattr(config,"LIBREOFFICE_PATH","missing-soffice")
    with pytest.raises(ValueError,match="指定的转换程序不存在"):
        legacy_word.converter_path()


@pytest.mark.parametrize("payload",[b"",b"wrong",b"PK\x03\x04wrong-docx-renamed"])
def test_invalid_or_renamed_document_is_not_success(payload):
    with pytest.raises(ValueError,match="仅更改后缀"):
        load_document("a.doc",payload)


def test_no_output_or_invalid_output_is_not_success(converted,monkeypatch):
    monkeypatch.setattr(legacy_word,"_run",lambda *_,**__:None)
    with pytest.raises(ValueError,match="未获得有效转换结果"):
        load_document("a.doc",legacy_word.OLE_HEADER)
    def invalid(command, **_):
        (Path(command[command.index("--outdir")+1])/"source.docx").write_bytes(b"bad")
    monkeypatch.setattr(legacy_word,"_run",invalid)
    with pytest.raises(ValueError,match="不是有效"):
        load_document("a.doc",legacy_word.OLE_HEADER)


def test_busy_slots_fail_clearly(converted,monkeypatch):
    class Busy:
        def acquire(self,**kwargs):
            return False
    monkeypatch.setattr(legacy_word,"_slots",Busy())
    with pytest.raises(ValueError,match="繁忙"):
        load_document("a.doc",legacy_word.OLE_HEADER)


def test_timeout_and_nonzero_exit(monkeypatch):
    class Process:
        pid=12345
        returncode=1
        def communicate(self,**kwargs):
            raise subprocess.TimeoutExpired("fake",1)
        def wait(self,**kwargs):
            pass
    monkeypatch.setattr(legacy_word.subprocess,"Popen",lambda *a,**kw:Process())
    monkeypatch.setattr(legacy_word.subprocess,"run",lambda *a,**kw:None)
    monkeypatch.setattr(legacy_word.os,"killpg",lambda *args:None,raising=False)
    with pytest.raises(ValueError,match="超时"):
        legacy_word._run(["fake"])
    monkeypatch.setattr(Process,"communicate",lambda *a,**kw:None)
    with pytest.raises(ValueError,match="转换失败"):
        legacy_word._run(["fake"])


def test_upload_old_word_through_existing_api(converted):
    engine=create_db_engine("sqlite://")
    try:
        with TestClient(create_app(MaterialStore(engine))) as client:
            result=client.post("/materials/upload",files={"file":("中文制度.doc",legacy_word.OLE_HEADER+b"fixture")})
            assert result.status_code==201
            material=result.json()["material"]
            assert material["file_type"]=="doc" and material["structure"]=="docx_styles"
            detail=client.get(f"/materials/{material['id']}").json()
            assert "650元" in detail["content"]
    finally:
        engine.dispose()



def profile_of(command):
    from urllib.parse import urlparse
    from urllib.request import url2pathname

    uri = next(c.split("=", 1)[1] for c in command if c.startswith("-env:UserInstallation="))
    return Path(url2pathname(urlparse(uri).path))


@pytest.fixture
def templated(converted, monkeypatch, tmp_path):
    """模拟 LibreOffice：初始化时像真程序一样重写 registrymodifications.xcu 并留下缓存文件。"""
    monkeypatch.setattr(config, "OFFICE_PROFILE_TEMPLATE", True)
    monkeypatch.setattr(legacy_word, "gettempdir", lambda: str(tmp_path / "temp"))
    monkeypatch.setattr(legacy_word, "_broken_templates", set())
    (tmp_path / "temp").mkdir()
    calls = {"init": 0, "profiles": [], "warm": [], "convert": legacy_word._run}

    def run(command, **kwargs):
        profile = profile_of(command)
        if command[-1].endswith("warm.csv"):
            calls["init"] += 1
            settings = profile / "user" / "registrymodifications.xcu"
            settings.write_text(settings.read_text(encoding="utf-8").replace(
                "</oor:items>", '<item oor:path="/org.openoffice.Setup/Office"><prop oor:name="ooSetupInstCompleted" '
                'oor:op="fuse"><value>true</value></prop></item></oor:items>'), encoding="utf-8")
            (profile / "user" / "cache").mkdir()
            (profile / "user" / "cache" / "warm").write_text("x", encoding="utf-8")
            return
        calls["profiles"].append(profile)
        calls["warm"].append((profile / "user" / "cache" / "warm").is_file())
        assert legacy_word._is_hardened(profile) and not (profile / "ready").exists()
        calls["convert"](command, **kwargs)
    monkeypatch.setattr(legacy_word, "_run", run)
    return calls


def test_conversion_copies_a_hardened_template_into_a_fresh_profile(templated, tmp_path):
    for _ in range(2):
        assert "| 杭州 | 480元 |" in load_document("制度.doc", legacy_word.OLE_HEADER + b"fixture").text
    assert templated["init"] == 1  # 模板只初始化一次
    first, second = templated["profiles"]
    assert first != second and not first.exists() and not second.exists()  # 每次独立配置目录，用后删除
    assert templated["warm"] == [True, True]
    template = next((tmp_path / "temp" / "briefly-office-profile").iterdir())
    assert (template / "user" / "cache" / "warm").is_file() and legacy_word._is_hardened(template)


def test_template_that_lost_macro_hardening_is_not_used(templated, tmp_path):
    load_document("制度.doc", legacy_word.OLE_HEADER + b"fixture")
    template = next((tmp_path / "temp" / "briefly-office-profile").iterdir())
    settings = template / "user" / "registrymodifications.xcu"
    settings.write_text(settings.read_text(encoding="utf-8").replace("<value>3</value>", "<value>0</value>"),
                        encoding="utf-8")
    assert not legacy_word._is_hardened(template)
    load_document("制度.doc", legacy_word.OLE_HEADER + b"fixture")
    # 第二次没有复制被改过的模板（没有初始化留下的缓存），而是从零建的加固配置
    assert templated["init"] == 1 and templated["warm"] == [True, False]


def test_failed_template_initialisation_falls_back_to_a_fresh_profile(converted, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "OFFICE_PROFILE_TEMPLATE", True)
    monkeypatch.setattr(legacy_word, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(legacy_word, "_broken_templates", set())
    convert, inits = legacy_word._run, []

    def run(command, **kwargs):
        if command[-1].endswith("warm.csv"):
            inits.append(command)
            raise ValueError("初始化超时")
        assert legacy_word._is_hardened(profile_of(command))
        convert(command, **kwargs)
    monkeypatch.setattr(legacy_word, "_run", run)
    for _ in range(2):
        assert "| 杭州 | 480元 |" in load_document("制度.doc", legacy_word.OLE_HEADER + b"fixture").text
    assert len(inits) == 1  # 失败后本进程不再反复初始化
