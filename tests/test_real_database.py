"""Checks against the real DDT4All database, when ecu.zip sits in the project folder (git-ignored).

Skipped otherwise: Renault's files are not part of the repository.
"""

from pathlib import Path

import pytest

from dusterscan import safety
from dusterscan.ddt import match, store
from dusterscan.ddt.reader import apply_names, dtc_record_requests, readable_requests
from dusterscan.renault import parse_renault_ident

from tests.test_ddt import WRITE_SIDS
from tests.test_real_duster import ABS_1902, CLUSTER_1902, IDENTS, read_module

ZIP = Path(__file__).resolve().parent.parent / "ecu.zip"
pytestmark = pytest.mark.skipif(not ZIP.exists(), reason="ecu.zip (DDT4All database) not in the project folder")

EXPECTED = {
    ("engine", "7E0", "7E8"): ("EMS312X_RDC_xxx_RDE_320_470_5F0_600_790_850_V1.1_20200116T154114.json", "exact"),
    ("cluster", "743", "763"): ("CLUSTER_X52_2nd_Indus_v3.3_20160120T132838.json", "exact"),
    ("airbag", "752", "772"): ("RSAT4_ACU_eng_v15_20150511T131328.json", "exact"),
    ("body", "745", "765"): ("BCM_T4_VS_DDT_16.5_20171221_20171221T171512.json", "same_software"),
    ("abs", "740", "760"): None,
}


@pytest.fixture(scope="module")
def db():
    return store.open_zip(ZIP)


def test_the_real_duster_gets_the_files_matched_by_hand(db):
    for (kind, tx, rx), expected in EXPECTED.items():
        ecu = {"kind": kind, "tx": tx, "rx": rx, "responded": True,
               "identification": parse_renault_ident(bytes.fromhex(IDENTS[kind]))}
        assert match.auto_choice(db, ecu) == expected, kind
        if expected:
            assert db.definition_ids(expected[0]) == (tx, rx)


def test_matched_definitions_only_yield_reads(db):
    for expected in filter(None, EXPECTED.values()):
        d = db.definition(expected[0])
        reads = readable_requests(d)
        assert reads, expected[0]
        for r in reads:
            assert int(r["sentbytes"][:2], 16) in (0x21, 0x22)
            safety.check_command(r["sentbytes"])
        for r in dtc_record_requests(d):
            assert r["sentbytes"].startswith("1906") and len(r["sentbytes"]) == 12
        for r in d["requests"]:
            if int(r["sentbytes"][:2], 16) in WRITE_SIDS:
                assert r not in reads
                with pytest.raises(safety.WriteBlockedError):
                    safety.check_command(r["sentbytes"])


def test_renault_names_for_the_real_cluster_codes(db):
    cluster = read_module("cluster", 0x743, CLUSTER_1902)
    apply_names(cluster, db.definition(EXPECTED[("cluster", "743", "763")][0]))
    named = {d["code"]: d.get("renault_name") for d in cluster["dtcs"]}
    assert set(named) == {"U0001", "B1404", "U3003", "B1412"}
    assert any(named.values())   # the cluster's own list names at least some of them
    abs_ = read_module("abs", 0x740, ABS_1902)
    assert all("renault_name" not in d for d in abs_["dtcs"])   # generic: no definition applied
