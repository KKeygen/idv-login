import os
import sys
import unittest


SRC_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from mpay_request_policy import (
    ROLE_BRIDGED_GAME,
    ROLE_HOSTED_FEVER_MPAY,
    ROLE_NATIVE_GAME,
    ROLE_REAL_FEVER,
    classify_mpay_request,
)


class Request:
    def __init__(self, query=None, body=b"", content_type=""):
        self.query = query or {}
        self.content = body
        self.headers = {"content-type": content_type}


class MpayRequestPolicyTests(unittest.TestCase):
    def test_native_pc_game_uses_released_policy(self):
        request = Request({"game_id": "aecfrt3rmaaaaajl-g-h55"})
        self.assertEqual(classify_mpay_request(request), ROLE_NATIVE_GAME)

    def test_real_fever_is_detected_by_destination_game(self):
        request = Request({"game_id": "g-a50", "dst_jf_game_id": "h55"})
        self.assertEqual(classify_mpay_request(request), ROLE_REAL_FEVER)
        self.assertEqual(
            classify_mpay_request(request, hosted_mpay_active=True),
            ROLE_HOSTED_FEVER_MPAY,
        )
        real = Request({"game_id": "h55", "dst_jf_game_id": "h55"})
        self.assertEqual(classify_mpay_request(real), ROLE_REAL_FEVER)

    def test_hosted_mpay_is_detected_from_form_body(self):
        request = Request(
            body=b"game_id=aecglf6ee4aaaarz-g-a50&app_channel=a50_sdk_cn",
            content_type="application/x-www-form-urlencoded",
        )
        self.assertEqual(
            classify_mpay_request(request, hosted_mpay_active=True),
            ROLE_HOSTED_FEVER_MPAY,
        )

    def test_active_target_game_is_vanilla_bridge_traffic(self):
        request = Request({"game_id": "prefix-g-h55"})
        self.assertEqual(
            classify_mpay_request(request, {"h55"}), ROLE_BRIDGED_GAME
        )


if __name__ == "__main__":
    unittest.main()
