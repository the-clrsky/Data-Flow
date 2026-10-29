"""Pre-flight checks run before a sync starts: connectivity to both
databases, plus the read/write privileges the sync will need. Everything
runs before any table is touched, so a permissions or connectivity problem
is reported as one clear list of failures instead of surfacing deep inside
the sync loop after partial work has already happened.

Disk space estimation is explicitly out of scope for this pass.
"""
from . import oracle_client as ora
from . import pg_client as pg
from . import mapping


def run(ocfg, pcfg, table_names):
    """Returns a list of failure dicts (empty if everything passed). Each
    failure has `check`, `table` (None for connection-level checks) and
    `error`. Every table is checked even if earlier ones failed, so the
    caller sees the full picture in one shot rather than one-at-a-time."""
    failures = []
    schema = pcfg["target_schema"]

    try:
        ocon = ora.connect(ocfg)
    except Exception as e:  # noqa: BLE001
        failures.append(dict(check="oracle_connection", table=None, error=str(e)))
        ocon = None

    try:
        pcon = pg.connect(pcfg)
    except Exception as e:  # noqa: BLE001
        failures.append(dict(check="postgres_connection", table=None, error=str(e)))
        pcon = None

    try:
        if ocon is not None:
            owner = ora.owner(ocfg)
            for t in {mapping.oracle_table(k) for k in table_names}:
                try:
                    ora.check_read_access(ocon, owner, t)
                except Exception as e:  # noqa: BLE001
                    failures.append(dict(check="oracle_read_access", table=t, error=str(e)))

        if pcon is not None:
            for t in table_names:
                pgt = mapping.get(t)["pg_table"] or mapping.default_table_name(t)
                try:
                    ok, reason = pg.check_privileges(pcon, schema, pgt)
                    if not ok:
                        failures.append(dict(check="postgres_privileges", table=t, error=reason))
                except Exception as e:  # noqa: BLE001
                    failures.append(dict(check="postgres_privileges", table=t, error=str(e)))
    finally:
        if ocon is not None:
            ocon.close()
        if pcon is not None:
            pcon.close()

    return failures
