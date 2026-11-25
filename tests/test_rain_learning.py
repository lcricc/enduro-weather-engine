import csv
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb
from calibrate_rain import CONFIG, fit, project, update


def csv_input(header, rows):
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(header.split())
    writer.writerows(rows)
    return io.BytesIO(output.getvalue().encode())


class LearningTest(unittest.TestCase):
    def test_latest_eligible_cycle_and_observations_across_archives(self):
        config = json.loads(CONFIG.read_text())
        end = datetime(2026,11,21,17,tzinfo=timezone.utc)
        hour = timedelta(hours=1)
        with tempfile.TemporaryDirectory() as work:
            db = duckdb.connect()
            db.execute("SET TimeZone='UTC'")
            def save(label, kind, header, rows):
                path = Path(work)/(label+".parquet")
                project(db,csv_input(header,rows),"rows.csv",path,kind,config)
                return path
            forecasts = []
            def cycle(hours_before, wet_models, count, published):
                issue = end - hour*hours_before
                rows = [["net","123",42.1,12.5,m,issue.isoformat(),end.isoformat(),int(i<wet_models)*0.1]
                        for i,m in enumerate(config["models"][:count])]
                path = save(str(hours_before),"forecasts","network station_id latitude longitude model acquired_utc valid_time_utc precipitation_mm",rows)
                forecasts.append((path, published))
            cycle(25, 8, 8, end-25*hour)
            cycle(6, 0, 8, end-5*hour)
            cycle(5, 2, 8, end-4*hour)
            cycle(4, 6, 7, end-3*hour)
            cycle(3, 8, 8, end-hour)
            obs = []
            for i, minutes in enumerate((30,30,0)):
                t = end-timedelta(minutes=minutes)
                row = ["net","123",t.isoformat(),"B13011","[1,null,null,null]","[1,0,1800]",0.05,"mm"]
                obs.append(save("obs"+str(i),"observations","network station_id time_utc variable level timerange value unit",[row]))
            result = update(config,fit(db,obs,forecasts,end,config))
            self.assertEqual(result["total"],[0,0,1,0,0,0,0,0,0])
            self.assertEqual(result["wet"],result["total"])
            self.assertEqual(result["calibration"]["through"],"2026-11-21")
