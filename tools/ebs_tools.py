import json
from pathlib import Path
from tools.cost_calculator import estimate_ebs_gp2_to_gp3_savings, EBS_PRICE_PER_GB
from tools.aws_client import USE_LIVE, get_client

_MOCK = json.loads((Path(__file__).parent.parent / "data" / "mock_ebs.json").read_text())


# ── public tools ───────────────────────────────────────────────────────────────

def get_ebs_volumes() -> str:
    """Return all EBS volumes with type, size, attachment state, and monthly cost."""
    return _live_volumes() if USE_LIVE else json.dumps(_MOCK["volumes"], indent=2)


def get_ebs_snapshots() -> str:
    """Return all EBS snapshots, flagging orphaned ones whose source volume no longer exists."""
    return _live_snapshots() if USE_LIVE else json.dumps(_MOCK["snapshots"], indent=2)


def get_unused_amis() -> str:
    """Return custom AMIs not referenced by any running or stopped instance."""
    return _live_amis() if USE_LIVE else json.dumps(_MOCK["amis"], indent=2)


def analyze_ebs_optimization() -> str:
    """
    Analyse volumes: unattached (immediate waste), gp2→gp3 migration (20% cheaper),
    and io1 volumes with low actual IOPS vs provisioned.
    """
    volumes = json.loads(_live_volumes() if USE_LIVE else json.dumps(_MOCK["volumes"]))
    return _run_ebs_analysis(volumes)


def analyze_orphaned_snapshots_and_amis() -> str:
    """Return orphaned snapshots and unused AMIs with cleanup actions and monthly cost."""
    snapshots = json.loads(_live_snapshots() if USE_LIVE else json.dumps(_MOCK["snapshots"]))
    amis = json.loads(_live_amis() if USE_LIVE else json.dumps(_MOCK["amis"]))
    orphaned_snaps = [s for s in snapshots if not s["associated_volume_exists"]]
    unused_amis = [a for a in amis if not a["is_used_by_any_instance"]]
    total = sum(s["monthly_cost_usd"] for s in orphaned_snaps) + sum(a["monthly_cost_usd"] for a in unused_amis)
    return json.dumps({
        "orphaned_snapshots": orphaned_snaps,
        "unused_amis": unused_amis,
        "total_monthly_savings_usd": round(total, 2),
    }, indent=2)


# ── shared analysis (works on either mock or live data) ────────────────────────

def _run_ebs_analysis(volumes: list) -> str:
    results = {"unattached_volumes": [], "gp2_to_gp3_candidates": [], "overprovisioned_iops": []}
    total_savings = 0.0

    for vol in volumes:
        if vol["state"] == "available" and not vol.get("attached_to"):
            results["unattached_volumes"].append({
                "volume_id": vol["volume_id"],
                "name": vol.get("name", vol["volume_id"]),
                "size_gb": vol["size_gb"],
                "monthly_cost_usd": vol["monthly_cost_usd"],
                "recommendation": "DELETE or snapshot-then-delete",
            })
            total_savings += vol["monthly_cost_usd"]
        elif vol["volume_type"] == "gp2":
            s = estimate_ebs_gp2_to_gp3_savings(vol["size_gb"])
            results["gp2_to_gp3_candidates"].append({
                "volume_id": vol["volume_id"],
                "name": vol.get("name", vol["volume_id"]),
                "size_gb": vol["size_gb"],
                **s,
            })
            total_savings += s["monthly_savings_usd"]
        elif vol["volume_type"] in ("io1", "io2") and vol.get("actual_avg_iops_used"):
            provisioned = vol["iops"]
            actual = vol["actual_avg_iops_used"]
            if actual < provisioned * 0.3:
                results["overprovisioned_iops"].append({
                    "volume_id": vol["volume_id"],
                    "name": vol.get("name", vol["volume_id"]),
                    "provisioned_iops": provisioned,
                    "actual_avg_iops": actual,
                    "utilization_percent": round(actual / provisioned * 100, 1),
                    "recommendation": "Migrate to gp3",
                    "monthly_cost_usd": vol["monthly_cost_usd"],
                })

    results["total_estimated_monthly_savings_usd"] = round(total_savings, 2)
    return json.dumps(results, indent=2)


# ── live implementations ───────────────────────────────────────────────────────

def _live_volumes() -> str:
    ec2 = get_client("ec2")
    volumes = []
    paginator = ec2.get_paginator("describe_volumes")
    for page in paginator.paginate():
        for v in page["Volumes"]:
            attachments = v.get("Attachments", [])
            attached_to = attachments[0]["InstanceId"] if attachments else None
            vtype = v.get("VolumeType", "gp2")
            size_gb = v.get("Size", 0)
            cost = round(size_gb * EBS_PRICE_PER_GB.get(vtype, 0.10), 2)
            tags = {t["Key"]: t["Value"] for t in v.get("Tags", [])}
            volumes.append({
                "volume_id": v["VolumeId"],
                "volume_type": vtype,
                "size_gb": size_gb,
                "state": v.get("State", "available"),
                "attached_to": attached_to,
                "name": tags.get("Name", v["VolumeId"]),
                "create_time": v.get("CreateTime", "").isoformat() if v.get("CreateTime") else "",
                "availability_zone": v.get("AvailabilityZone", ""),
                "monthly_cost_usd": cost,
                "iops": v.get("Iops", 0),
                "throughput_mbps": v.get("Throughput"),
            })
    return json.dumps(volumes, indent=2)


def _live_snapshots() -> str:
    ec2 = get_client("ec2")

    # Get all volume IDs that currently exist
    existing_volumes = set()
    vol_paginator = ec2.get_paginator("describe_volumes")
    for page in vol_paginator.paginate():
        for v in page["Volumes"]:
            existing_volumes.add(v["VolumeId"])

    snapshots = []
    snap_paginator = ec2.get_paginator("describe_snapshots")
    for page in snap_paginator.paginate(OwnerIds=["self"]):
        for s in page["Snapshots"]:
            size_gb = s.get("VolumeSize", 0)
            cost = round(size_gb * 0.05, 2)  # $0.05/GB/month for EBS snapshots
            vol_exists = s.get("VolumeId", "") in existing_volumes
            tags = {t["Key"]: t["Value"] for t in s.get("Tags", [])}
            snapshots.append({
                "snapshot_id": s["SnapshotId"],
                "volume_id": s.get("VolumeId", ""),
                "volume_size_gb": size_gb,
                "state": s.get("State", "completed"),
                "start_time": s.get("StartTime", "").isoformat() if s.get("StartTime") else "",
                "description": s.get("Description", ""),
                "associated_volume_exists": vol_exists,
                "monthly_cost_usd": cost,
                "name": tags.get("Name", s["SnapshotId"]),
            })
    return json.dumps(snapshots, indent=2)


def _live_amis() -> str:
    ec2 = get_client("ec2")

    # Which AMIs are in use by any instance?
    in_use = set()
    inst_paginator = ec2.get_paginator("describe_instances")
    for page in inst_paginator.paginate():
        for r in page["Reservations"]:
            for inst in r["Instances"]:
                if inst.get("ImageId"):
                    in_use.add(inst["ImageId"])

    amis = []
    resp = ec2.describe_images(Owners=["self"])
    for img in resp.get("Images", []):
        snap_ids = [
            bdm["Ebs"]["SnapshotId"]
            for bdm in img.get("BlockDeviceMappings", [])
            if "Ebs" in bdm and "SnapshotId" in bdm["Ebs"]
        ]
        # Approximate cost: $0.10/GB/month for backing snapshots
        total_size_gb = sum(
            bdm.get("Ebs", {}).get("VolumeSize", 0)
            for bdm in img.get("BlockDeviceMappings", [])
            if "Ebs" in bdm
        )
        tags = {t["Key"]: t["Value"] for t in img.get("Tags", [])}
        amis.append({
            "image_id": img["ImageId"],
            "name": img.get("Name", img["ImageId"]),
            "creation_date": img.get("CreationDate", ""),
            "state": img.get("State", "available"),
            "is_used_by_any_instance": img["ImageId"] in in_use,
            "snapshot_ids": snap_ids,
            "monthly_cost_usd": round(total_size_gb * 0.10, 2),
        })
    return json.dumps(amis, indent=2)
