"""The Windows start-up entry names the signed-in person, so a normal window may create it."""
from speakstone_worker import service


def test_the_entry_names_the_person_on_the_trigger_and_the_principal():
    xml = service.build_task_xml("C:/a/app.exe", ["--background"], user="MYPC\\sam")
    assert "<LogonTrigger><Enabled>true</Enabled><UserId>MYPC\\sam</UserId></LogonTrigger>" in xml
    assert '<Principal id="Author"><UserId>MYPC\\sam</UserId>' in xml


def test_a_name_with_special_characters_is_escaped():
    xml = service.build_task_xml("C:/a/app.exe", [], user="PC\\a&b")
    assert "PC\\a&amp;b" in xml and "a&b<" not in xml


def test_with_no_name_known_the_entry_is_as_before():
    xml = service.build_task_xml("C:/a/app.exe", [], user="")
    assert "<UserId>" not in xml and "<LogonTrigger><Enabled>true</Enabled></LogonTrigger>" in xml


def test_windows_user_reads_the_environment():
    assert service.windows_user({"USERNAME": "sam", "USERDOMAIN": "MYPC"}) == "MYPC\\sam"
    assert service.windows_user({"USERNAME": "sam"}) == "sam"
    assert service.windows_user({}) == ""
