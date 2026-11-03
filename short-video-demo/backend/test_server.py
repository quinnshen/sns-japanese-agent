import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import server


class SavedResultTests(unittest.TestCase):
    def test_video_key_ignores_tracking_query_but_not_video_id(self):
        self.assertEqual(server.video_key("https://www.youtube.com/shorts/AbCdEf012_-?feature=share"), "AbCdEf012_-")
        self.assertEqual(server.video_key("https://www.youtube.com/watch?v=AbCdEf012_-&t=3"), "AbCdEf012_-")
        self.assertIsNone(server.video_key("https://example.com/watch?v=AbCdEf012_-"))

    def test_latest_saved_result_can_be_loaded_without_recapture(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "saved.json"
            path.write_text(json.dumps({
                "id": "saved", "source": {"type": "youtube", "url": "https://www.youtube.com/shorts/AbCdEf012_-"},
                "sentences": [{"id": "s0001", "text": "はい。"}],
            }), encoding="utf-8")
            with patch.object(server, "DATA_DIR", Path(directory)):
                result = server.latest_result_for_url("https://www.youtube.com/watch?v=AbCdEf012_-&t=3")
            self.assertEqual(result["id"], "saved")


if __name__ == "__main__":
    unittest.main()
