import csv
import gzip
import json
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

from enduro_rain_v01 import CONFIG, write_issue, validate_issues


class RainForecastTest(unittest.TestCase):
    def test_prospective_record_and_matching(self):
        now = datetime.now(timezone.utc).replace(microsecond=0)
        start = now.replace(minute=0, second=0) + timedelta(hours=3)
        hour = timedelta(hours=1)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            def issue(run, acquired, published, wet_models):
                folder = root / f"meteohub-forecasts-{run}"
                folder.mkdir()
                (folder / "published_at.txt").write_text(published.isoformat())
                raw = folder / "input.gz"
                with gzip.open(raw, "wt") as f:
                    out = csv.writer(f)
                    out.writerow(["network","station_id","model","valid_time_utc","precipitation_mm"])
                    for station in ["complete","incomplete","duplicate","negative"]:
                        for i, name in enumerate(CONFIG["models"]):
                            if station == "incomplete" and i == 7:
                                continue
                            mm = -1 if station=="negative" and i==7 else (0.1 if i<wet_models else 0)
                            row = ["net",station,name,(start+hour).isoformat(),mm]
                            out.writerow(row)
                            if station=="duplicate" and i==0:
                                out.writerow(row)
                dest=folder / "enduro_rain_v01_test.json.gz"
                self.assertEqual(write_issue(raw, acquired, dest),1)
                with gzip.open(dest, "rt") as f:
                    p=json.load(f)["predictions"][0]
                    self.assertEqual(p[3],wet_models)
                    self.assertEqual(p[4],(CONFIG["wet"][wet_models]+1)/(CONFIG["total"][wet_models]+2))
            issue(1,start-24*hour,now,3)
            issue(2,now,now,5)
            issue(3,now+timedelta(seconds=1),start,8)
            obs={("net","complete",start+hour,"rain"):0.1}
            result=validate_issues(root,obs,"rain",start+hour)
            self.assertEqual(result["overall"]["station_hours"],1)
            self.assertEqual(result["overall"]["5_of_8"]["hits"],1)
            self.assertEqual(result["overall"]["6_of_8"]["misses"],1)
            self.assertEqual(validate_issues(root,{},"rain",start+hour)["awaiting_measurement"],1)
            self.assertEqual(validate_issues(root,obs,"rain",start)["elapsed_station_hours"],0)
