
#!/usr/bin/env python3
"""ENDURO WEATHER ENGINE - Model Accuracy Analysis V1.

Analizza i risultati del validatore MeteoHub V3.2.
Non modifica i dati originali e non assegna pesi ai modelli.
"""

import csv
import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VALIDATION = ROOT / "data/validation/meteohub"
OUTPUT = ROOT / "data/analysis/model_accuracy"

SOURCE = VALIDATION / "validation_latest.json"

MODELS = [
    "ecmwf_ifs025",
    "ecmwf_aifs025_single",
    "icon_seamless",
    "gfs_seamless",
    "meteofrance_seamless",
    "ukmo_seamless",
    "gem_seamless",
    "arpege_world",
]

VARIABLES = [
    "temperature_2m_c",
    "wind_speed_10m_kmh",
]

BUCKETS = [
    "0-24h",
    "24-48h",
    "48-72h",
    "72-96h",
]

# Soglie operative provvisorie, non validazione scientifica.
MIN_EXPLORATORY = 30
MIN_REVIEW = 200

FIELDS = [
    "model",
    "variable",
    "lead_bucket",
    "sample_type",
    "n",
    "mae",
    "rmse",
    "bias",
    "sample_status",
    "comparable",
]


def finite_number(value):
    try:
        number = float(value)
        if math.isfinite(number):
            return number
    except (ValueError, TypeError):
        pass
    return None


def sample_status(n):
    if n < MIN_EXPLORATORY:
        return "INSUFFICIENT"
    if n < MIN_REVIEW:
        return "EXPLORATORY"
    return "REVIEW_REQUIRED"


def normalize_metric(item, sample_type):
    model = item.get("model")
    variable = item.get("variable")
    bucket = item.get("lead_bucket")

    if (
        model not in MODELS
        or variable not in VARIABLES
        or bucket not in BUCKETS
    ):
        return None

    try:
        n = int(item.get("n", 0))
    except (TypeError, ValueError):
        return None

    mae = finite_number(item.get("mae"))
    rmse = finite_number(item.get("rmse"))
    bias = finite_number(item.get("bias"))

    if (
        n <= 0
        or mae is None
        or rmse is None
        or bias is None
        or mae < 0
        or rmse < 0
        or abs(bias) > mae + 0.001
        or rmse + 0.001 < mae
    ):
        return None

    return {
        "model": model,
        "variable": variable,
        "lead_bucket": bucket,
        "sample_type": sample_type,
        "n": n,
        "mae": mae,
        "rmse": rmse,
        "bias": bias,
        "sample_status": sample_status(n),
        "comparable": sample_type == "OFFICIAL",
    }


def analyze():
    if not SOURCE.is_file():
        raise FileNotFoundError(
            "Rapporto V3.2 non trovato: " + str(SOURCE)
        )

    with SOURCE.open(encoding="utf-8") as source:
        validation = json.load(source)

    if validation.get("version") != "MeteoHub Validation V3.2":
        raise RuntimeError(
            "Versione del validatore non compatibile"
        )

    official = validation.get("metrics", [])
    preliminary = validation.get("preliminary_metrics", [])

    if not isinstance(official, list):
        raise ValueError("Metriche ufficiali non valide")

    if not isinstance(preliminary, list):
        raise ValueError("Metriche preliminari non valide")

    rows = []
    invalid = 0
    seen = set()

    for sample_type, collection in [
        ("OFFICIAL", official),
        ("PRELIMINARY", preliminary),
    ]:
        for item in collection:
            if not isinstance(item, dict):
                invalid += 1
                continue

            row = normalize_metric(item, sample_type)

            if row is None:
                invalid += 1
                continue

            key = (
                row["model"],
                row["variable"],
                row["lead_bucket"],
                sample_type,
            )

            if key in seen:
                raise ValueError(
                    "Metrica duplicata: " + str(key)
                )

            seen.add(key)
            rows.append(row)

    # Un confronto ufficiale richiede tutti gli otto modelli
    # sul medesimo insieme di eventi.
    official_groups = defaultdict(list)

    for row in rows:
        if row["sample_type"] == "OFFICIAL":
            key = (row["variable"], row["lead_bucket"])
            official_groups[key].append(row)

    comparison_groups = []

    for key, group in sorted(official_groups.items()):
        models = {row["model"] for row in group}
        counts = {row["n"] for row in group}

        complete = (
            models == set(MODELS)
            and len(group) == len(MODELS)
            and len(counts) == 1
        )

        comparison_groups.append({
            "variable": key[0],
            "lead_bucket": key[1],
            "models_present": len(models),
            "sample_counts": sorted(counts),
            "complete_eight_models": complete,
        })

        if not complete:
            for row in group:
                row["comparable"] = False

    rows.sort(
        key=lambda row: (
            row["variable"],
            BUCKETS.index(row["lead_bucket"]),
            row["sample_type"],
            row["model"],
        )
    )

    official_comparisons = sum(
        item["complete_eight_models"]
        for item in comparison_groups
    )

    report = {
        "engine": "ENDURO WEATHER ENGINE",
        "version": "Model Accuracy Analysis V1",
        "generated_utc": datetime.now(
            timezone.utc
        ).isoformat(),
        "source_validation_version": validation["version"],
        "source_generated_utc": validation.get(
            "generated_utc"
        ),
        "source_status": validation.get("status"),
        "source_preliminary_status": validation.get(
            "preliminary_status"
        ),
        "source_observation_matching": validation.get(
            "observation_matching"
        ),
        "models_expected": MODELS,
        "variables": VARIABLES,
        "lead_buckets": BUCKETS,
        "official_metric_rows": sum(
            row["sample_type"] == "OFFICIAL"
            for row in rows
        ),
        "preliminary_metric_rows": sum(
            row["sample_type"] == "PRELIMINARY"
            for row in rows
        ),
        "invalid_metric_rows": invalid,
        "complete_comparison_groups": official_comparisons,
        "comparison_groups": comparison_groups,
        "metrics": rows,
        "ensemble_weights_ready": False,
        "limitations": [
            "Le metriche preliminari non sono classifiche ufficiali.",
            "Solo i confronti ufficiali usano eventi comuni.",
            "Le soglie dei campioni sono operative e provvisorie.",
            "La dimensione del campione non garantisce indipendenza statistica.",
            "Non sono ancora disponibili intervalli di confidenza.",
            "La precipitazione richiede validazione BUFR dedicata.",
            "Nessun peso del modello viene calcolato.",
            "La verifica per quota e orografia non è ancora attiva.",
        ],
    }

    OUTPUT.mkdir(parents=True, exist_ok=True)

    json_path = OUTPUT / "accuracy_latest.json"
    csv_path = OUTPUT / "accuracy_metrics.csv"

    json_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    with csv_path.open(
        "w", newline="", encoding="utf-8"
    ) as destination:
        writer = csv.DictWriter(
            destination, fieldnames=FIELDS
        )
        writer.writeheader()
        writer.writerows(rows)

    summary = {
        "version": report["version"],
        "official_metric_rows": report[
            "official_metric_rows"
        ],
        "preliminary_metric_rows": report[
            "preliminary_metric_rows"
        ],
        "complete_comparison_groups": report[
            "complete_comparison_groups"
        ],
        "invalid_metric_rows": invalid,
        "ensemble_weights_ready": False,
    }

    print(json.dumps(summary, indent=2))
    print("Report JSON:", json_path)
    print("Report CSV:", csv_path)


if __name__ == "__main__":
    analyze()
