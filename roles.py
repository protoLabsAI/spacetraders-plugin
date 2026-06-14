"""Pure fleet role-classification — which job each hull is suited for, decided from
the LIVE ship object (its mounts + cargo hold), not a name or hull-type guess.

No I/O and no relative imports, so this unit-tests host-free exactly like ``client.py``
(``import roles``). The engine (``fleet.autopilot``) and the read-only surfaces
(``tools.st_autopilot_status``, ``dashboard``) all classify through here, so the label
the operator sees and the job the engine assigns can never disagree.

Why this module exists: a mining drone has a (small) cargo hold, so a capacity-only
split — "has a hold ⇒ trade/contract, else scout" — swept drones into the trade
rotation and the survey→extract loop was never dispatched. Roles are about CAPABILITY
(does it carry a mining laser?), not just whether a hold exists.
"""

from __future__ import annotations


def can_mine(ship: dict) -> bool:
    """True iff the ship carries an ore-extraction mount (a mining laser).

    That is the SpaceTraders rule for "can this hull run the survey→extract loop":
    a 15-cargo MINING_DRONE qualifies, a 40-cargo LIGHT_HAULER does not, and the
    mine+haul COMMAND frigate does. Siphon drones (``MOUNT_GAS_SIPHON_*``, gas giants)
    are deliberately excluded — they don't mine ore. Reads ``ship["mounts"]`` from the
    live ``/my/ships`` object and tolerates a missing/partial mounts list.
    """
    return any("MINING_LASER" in (m.get("symbol") or "")
               for m in ship.get("mounts", []) or [])


def _capacity(ship: dict) -> int:
    return (ship.get("cargo") or {}).get("capacity", 0) or 0


# The roles an operator can PIN a ship to via st_assign, overriding the auto-classifier.
# "auto" (or absent) = let assign_roles decide; "contract" = force to the front of the
# trader list (the lead works contracts); "idle" = park it, no job this window.
ROLE_NAMES = {"auto", "mine", "trade", "contract", "scout", "idle"}


def assign_roles(ships: list, *, mining_enabled: bool = True,
                 overrides: dict | None = None) -> dict:
    """Partition a fleet into the three roles the engine knows how to drive:

      * ``probe``  — no cargo hold (flies free) → scouts the price map
      * ``miner``  — carries a mining laser → runs the survey→extract→sell loop
      * ``trader`` — has a hold, no mining laser → contracts (capital base) + arbitrage

    ``overrides`` is ``{ship_symbol: role}`` from st_assign — an OPERATOR PIN that beats
    the auto-classifier (this is the OODA strategist's per-ship control): ``mine`` →
    miner, ``trade`` → trader, ``contract`` → lead trader (front of the list), ``scout``
    → probe (even a hauler can be parked as a price feed), ``idle`` → no job, ``auto``/
    absent → classified normally below.

    ``mining_enabled`` is the strategy lever (see ``strategy.py``): when False (e.g. the
    ``trade-max`` preset) nothing is AUTO-sent mining — every unpinned hold trades — so
    the whole fleet pushes arbitrage. Explicit ``mine`` pins are still honoured.

    Capital-base guard (KB ``zero-to-million``: contracts are the capital base and need a
    hold): if no ship ends up a trader but AUTO-classified miners exist — e.g. a fresh
    agent whose only ship is the mine+haul COMMAND frigate — promote the largest-hold
    AUTO miner to trader so the contract/trade lever still runs. An EXPLICIT ``mine`` pin
    is never drafted away (the operator's call wins).

    Pure: returns lists referencing the same ship dicts, order preserved (so ``trader[0]``
    — the contract worker — stays the first pinned-contract or first-acquired hauler).
    """
    overrides = overrides or {}
    probes, miners, traders, lead, auto = [], [], [], [], []
    for s in ships:
        r = overrides.get(s.get("symbol"), "auto")
        if r == "idle":
            continue
        elif r == "scout":
            probes.append(s)
        elif r == "mine":
            miners.append(s)
        elif r == "contract":
            lead.append(s)
        elif r == "trade":
            traders.append(s)
        else:
            auto.append(s)
    probes += [s for s in auto if _capacity(s) == 0]
    auto_cargo = [s for s in auto if _capacity(s) > 0]
    if mining_enabled:
        auto_miners = [s for s in auto_cargo if can_mine(s)]
        auto_traders = [s for s in auto_cargo if not can_mine(s)]
    else:
        auto_miners, auto_traders = [], list(auto_cargo)
    miners += auto_miners
    traders = lead + traders + auto_traders
    if not traders and auto_miners:
        draft = sorted(auto_miners, key=_capacity, reverse=True)[0]
        traders = [draft]
        miners = [m for m in miners if m is not draft]
    return {"probes": probes, "miners": miners, "traders": traders}
