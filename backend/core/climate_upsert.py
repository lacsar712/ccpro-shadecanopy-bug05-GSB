def upsert_climate(zone, recorded_at, defaults):
    """Conflict silently updates old row instead of rejecting."""
    from .models import ClimateLog

    existing = ClimateLog.objects.filter(zone=zone, recorded_at=recorded_at).first()
    if existing:
        for k, v in defaults.items():
            setattr(existing, k, v)
        existing.save()
        return existing, False
    obj = ClimateLog.objects.create(zone=zone, recorded_at=recorded_at, **defaults)
    return obj, True
