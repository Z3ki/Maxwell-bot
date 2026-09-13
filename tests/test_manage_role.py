"""manage_role can reorder Discord role hierarchy (above/below/position)."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot_tools import (
    ManageRoleTool,
    _parse_role_placement,
    _plan_role_move,
)
from tool_schemas import TOOL_PARAMETERS


def _perms(**flags):
    flags.setdefault("administrator", False)
    flags.setdefault("manage_roles", False)
    return SimpleNamespace(**flags)


def _role(*, rid, name, position):
    role = SimpleNamespace(
        id=rid,
        name=name,
        position=position,
        colour=SimpleNamespace(value=0),
        hoist=False,
        mentionable=False,
        managed=False,
        members=[],
        permissions=SimpleNamespace(administrator=False),
        edit=AsyncMock(),
        delete=AsyncMock(),
    )
    role.is_default = lambda: name == "@everyone" or rid == 10
    return role


def _guild():
    everyone = _role(rid=10, name="@everyone", position=0)
    early = _role(rid=11, name="Early Supporter", position=1)
    members = _role(rid=12, name="Members", position=2)
    qmem = _role(rid=13, name="Q-Members", position=3)
    mods = _role(rid=14, name="Mods", position=4)
    bot_role = _role(rid=15, name="Maxwell", position=5)
    admin = _role(rid=16, name="Admin", position=6)
    roles = [everyone, early, members, qmem, mods, bot_role, admin]
    me = SimpleNamespace(
        id=1,
        display_name="Max",
        name="Max",
        guild_permissions=_perms(manage_roles=True),
        top_role=bot_role,
        roles=[everyone, bot_role],
    )
    asker = SimpleNamespace(
        id=5,
        display_name="Ada",
        name="Ada",
        guild_permissions=_perms(manage_roles=True),
        top_role=admin,
        roles=[everyone, admin],
        guild=None,
    )
    guild = SimpleNamespace(
        id=10,
        name="Villa",
        me=me,
        owner_id=9,
        roles=roles,
        get_role=lambda rid: next((r for r in roles if r.id == int(rid)), None),
        get_member=lambda uid: asker if int(uid) == 5 else None,
        edit_role_positions=AsyncMock(return_value=[]),
        create_role=AsyncMock(),
    )
    asker.guild = guild
    return SimpleNamespace(
        guild=guild,
        everyone=everyone,
        early=early,
        members=members,
        qmem=qmem,
        mods=mods,
        bot_role=bot_role,
        admin=admin,
        me=me,
        asker=asker,
        msg=SimpleNamespace(guild=guild, author=asker),
    )


def _positions(changes):
    return {role.name: pos for role, pos in changes}


def test_schema_documents_hierarchy_move():
    props = TOOL_PARAMETERS["manage_role"]["properties"]
    assert "position" in props
    assert "above" in props
    assert "below" in props
    action = props["action"]["description"].lower()
    assert "move" in action
    assert "above=q-members" in props["above"]["description"].lower()


def test_description_tells_the_model_to_move_instead_of_drag():
    desc = ManageRoleTool(SimpleNamespace()).get_description().lower()
    assert "move" in desc
    assert "above" in desc
    assert "below" in desc
    assert "position" in desc
    assert "server settings" in desc
    assert "admin-only" not in desc


def test_parse_role_placement_exclusive_and_int():
    kind, value, err = _parse_role_placement(position="3")
    assert err == ""
    assert kind == "position"
    assert value == 3
    kind, value, err = _parse_role_placement(above="Q-Members")
    assert (kind, value, err) == ("above", "Q-Members", "")
    _, _, err = _parse_role_placement(above="A", below="B")
    assert "only one" in err
    _, _, err = _parse_role_placement(position="1.5")
    assert "whole number" in err
    kind, _, err = _parse_role_placement()
    assert kind is None and err == ""


def test_plan_moves_early_supporter_above_q_members():
    fx = _guild()
    changes, summary, err = _plan_role_move(
        fx.guild, fx.me, fx.early, above="Q-Members"
    )
    assert err == ""
    assert _positions(changes) == {
        "Early Supporter": 3,
        "Members": 1,
        "Q-Members": 2,
        "Mods": 4,
    }
    assert "immediately above Q-Members" in summary
    assert "now pos 3" in summary


def test_plan_moves_early_supporter_below_q_members():
    fx = _guild()
    fx.early.position = 4
    fx.mods.position = 1
    fx.members.position = 2
    fx.qmem.position = 3
    # movable order by position: Mods 1, Members 2, Q-Members 3, Early 4
    changes, summary, err = _plan_role_move(
        fx.guild, fx.me, fx.early, below="Q-Members"
    )
    assert err == ""
    assert _positions(changes)["Early Supporter"] == 3
    assert _positions(changes)["Q-Members"] == 4
    assert "immediately below Q-Members" in summary


def test_plan_absolute_position():
    fx = _guild()
    changes, summary, err = _plan_role_move(
        fx.guild, fx.me, fx.early, position="4"
    )
    assert err == ""
    assert _positions(changes)["Early Supporter"] == 4
    assert "to position 4" in summary


def test_plan_already_above_is_noop():
    fx = _guild()
    # Put Early immediately above Q-Members first.
    fx.members.position = 1
    fx.qmem.position = 2
    fx.early.position = 3
    fx.mods.position = 4
    changes, summary, err = _plan_role_move(
        fx.guild, fx.me, fx.early, above="Q-Members"
    )
    assert err == ""
    assert changes == []
    assert "already immediately above" in summary


def test_plan_rejects_everyone_and_over_ceiling():
    fx = _guild()
    _, _, err = _plan_role_move(
        fx.guild, fx.me, fx.everyone, above="Q-Members"
    )
    assert "@everyone" in err
    _, _, err = _plan_role_move(
        fx.guild, fx.me, fx.early, above="Maxwell"
    )
    assert "cannot place a role above it" in err
    _, _, err = _plan_role_move(fx.guild, fx.me, fx.early, position="0")
    assert "at least 1" in err
    _, _, err = _plan_role_move(fx.guild, fx.me, fx.early, position="5")
    assert "equal/higher than my top role" in err
    _, _, err = _plan_role_move(
        fx.guild, fx.me, fx.early, above="Early Supporter"
    )
    assert "itself" in err
    _, _, err = _plan_role_move(fx.guild, fx.me, fx.early, below="10")
    assert "below @everyone" in err


def test_execute_move_above_calls_edit_role_positions():
    fx = _guild()
    result = asyncio.run(
        ManageRoleTool(SimpleNamespace()).execute(
            fx.msg,
            action="move",
            name="Early Supporter",
            above="Q-Members",
        )
    )
    assert result.startswith("Moved Early Supporter")
    assert "immediately above Q-Members" in result
    fx.guild.edit_role_positions.assert_awaited()
    payload = fx.guild.edit_role_positions.await_args.args[0]
    by_id = {int(key.id): pos for key, pos in payload.items()}
    assert by_id[11] == 3
    assert by_id[13] == 2
    assert by_id[12] == 1
    assert by_id[14] == 4
    assert 15 not in by_id  # bot's own role stays put
    assert 16 not in by_id  # admin stays put
    assert 10 not in by_id  # @everyone stays put


def test_execute_edit_with_above_reorders_without_role_edit():
    fx = _guild()
    result = asyncio.run(
        ManageRoleTool(SimpleNamespace()).execute(
            fx.msg,
            action="edit",
            name="Early Supporter",
            above="Q-Members",
        )
    )
    assert "Moved Early Supporter" in result
    fx.early.edit.assert_not_awaited()
    fx.guild.edit_role_positions.assert_awaited()


def test_execute_move_requires_a_placement():
    fx = _guild()
    result = asyncio.run(
        ManageRoleTool(SimpleNamespace()).execute(
            fx.msg, action="move", name="Early Supporter"
        )
    )
    assert result.startswith("Error:")
    assert "position, above, or below" in result
    fx.guild.edit_role_positions.assert_not_awaited()


def test_execute_unknown_action_mentions_move():
    fx = _guild()
    result = asyncio.run(
        ManageRoleTool(SimpleNamespace()).execute(
            fx.msg, action="paint", name="Early Supporter"
        )
    )
    assert "move" in result


def test_execute_reorder_alias_and_below_bot_role():
    fx = _guild()
    result = asyncio.run(
        ManageRoleTool(SimpleNamespace()).execute(
            fx.msg,
            action="reorder",
            name="Early Supporter",
            below="Maxwell",
        )
    )
    assert result.startswith("Moved Early Supporter")
    assert "immediately below Maxwell" in result
    payload = fx.guild.edit_role_positions.await_args.args[0]
    by_id = {int(key.id): pos for key, pos in payload.items()}
    assert by_id[11] == 4


def test_execute_refuses_without_manage_roles():
    fx = _guild()
    fx.me.guild_permissions = _perms(manage_roles=False)
    result = asyncio.run(
        ManageRoleTool(SimpleNamespace()).execute(
            fx.msg,
            action="move",
            name="Early Supporter",
            above="Q-Members",
        )
    )
    assert result.startswith("Error:")
    assert "manage_roles" in result
    fx.guild.edit_role_positions.assert_not_awaited()
