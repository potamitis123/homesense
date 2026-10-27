"""Go to time on the Audio Dashboard: the server half.

Landing on the page happens in the browser (assets/grid_time_jump.js); what
is tested here is which rows get loaded, when the grid parks, and that a
parked grid is left alone by the automatic reload.
"""
import os
import sys
from datetime import datetime
from unittest.mock import MagicMock, patch

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
sys.path.append(ROOT)
sys.path.append(os.path.join(ROOT, 'ast_dash'))
sys.modules.setdefault('dash_local_react_components', MagicMock())

# The page opens the PTT audio sessions on import: an MQTT connection to the
# broker and the microphone. Nothing here needs either.
with patch('dash.register_page'), patch('dash.get_app') as mock_get_app, \
        patch('ast_src.audio_transport.AudioSessionManager'):
    mock_get_app.return_value = MagicMock()
    from ast_dash.pages import audio_details as AD

NOW = datetime(2026, 9, 17, 12, 0, 0)
LIVE = ("2026-09-10 00:00:00", "2026-09-17 23:59:59")
PARKED = {"range": ["2026-08-29 00:00:00", "2026-09-04 23:59:59"], "at": "2026-09-01 14:05:00"}


def rows(*stamps):
    return [{"ID": 100 + i, "Timestamp": stamp, "Class 1": "Speech",
             "transcription": "[00:00.00 - 00:02.00] Voula: Έλα"} for i, stamp in enumerate(stamps)]


def find(component, wanted):
    if getattr(component, 'id', None) == wanted:
        return component
    children = getattr(component, 'children', None)
    for child in children if isinstance(children, list) else [children]:
        if child is not None and not isinstance(child, str):
            found = find(child, wanted)
            if found is not None:
                return found
    return None


# --- reading times and ranges ------------------------------------------

def test_a_typed_moment_is_read_with_or_without_seconds():
    assert AD.parse_jump_time("2026-09-16T14:05") == datetime(2026, 9, 16, 14, 5)
    assert AD.parse_jump_time("2026-09-16T14:05:36") == datetime(2026, 9, 16, 14, 5, 36)
    assert AD.parse_jump_time("2026-09-16 14:05:36.250") == datetime(2026, 9, 16, 14, 5, 36)
    assert AD.parse_jump_time("") is None
    assert AD.parse_jump_time(None) is None
    assert AD.parse_jump_time("yesterday") is None


def test_a_day_picked_in_the_calendar_is_included_whole():
    assert AD.query_range("2026-09-10", "2026-09-12", NOW) == ("2026-09-10 00:00:00", "2026-09-12 23:59:59")
    assert AD.query_range("2026-09-16", "2026-09-16", NOW) == ("2026-09-16 00:00:00", "2026-09-16 23:59:59")
    assert AD.query_range(*LIVE, NOW) == LIVE


def test_an_unusable_range_is_the_live_week():
    assert AD.live_range(NOW) == LIVE
    assert AD.query_range(None, None, NOW) == LIVE
    assert AD.query_range("2026-09-12", "2026-09-10", NOW) == LIVE
    assert AD.query_range("soon", "2026-09-10", NOW) == LIVE


def test_the_week_around_a_moment_never_runs_past_today():
    assert AD.range_around(datetime(2026, 9, 1, 14, 5), NOW) == ("2026-08-29 00:00:00", "2026-09-04 23:59:59")
    assert AD.range_around(datetime(2026, 9, 16, 14, 5), NOW) == ("2026-09-13 00:00:00", "2026-09-17 23:59:59")
    assert AD.range_around(datetime(2026, 9, 30, 9, 0), NOW) == ("2026-09-14 00:00:00", "2026-09-17 23:59:59")


def test_only_dates_chosen_in_the_calendar_count_as_picked():
    assert AD.picked_in_calendar("2026-09-10", "2026-09-12")
    assert AD.picked_in_calendar("2026-09-10 00:00:00", "2026-09-12")
    assert not AD.picked_in_calendar(*LIVE)
    assert not AD.picked_in_calendar(None, None)


# --- Go to time ----------------------------------------------------------

def test_a_moment_in_the_loaded_week_reloads_nothing_and_parks():
    with patch.object(AD, "get_db_rec") as fetch:
        plan = AD.plan_jump("2026-09-16T14:05", None, [], AD.VIEW_ALL, [], None, 0.25, NOW)
    fetch.assert_not_called()
    assert "rows" not in plan and "dates" not in plan
    assert plan["parked"] == {"range": list(LIVE), "at": "2026-09-16 14:05:00"}
    assert plan["jump"]["at"] == "2026-09-16 14:05:00" and plan["jump"]["rows"] is None


def test_a_moment_outside_it_loads_the_week_around_it():
    loaded = rows("2026-09-02 10:00:00", "2026-09-01 14:04:00")
    with patch.object(AD, "get_db_rec", return_value=(loaded, [{"field": "Timestamp"}], None, None)) as fetch:
        plan = AD.plan_jump("2026-09-01T14:05", None, [], AD.VIEW_ALL, [], "laughter", 0.25, NOW)
    # "laughter" was typed but the grid was not showing CLAP results
    fetch.assert_called_once_with("2026-08-29 00:00:00", "2026-09-04 23:59:59", None, 0.25)
    assert plan["dates"] == ("2026-08-29 00:00:00", "2026-09-04 23:59:59")
    assert plan["parked"] == {"range": ["2026-08-29 00:00:00", "2026-09-04 23:59:59"],
                              "at": "2026-09-01 14:05:00"}
    assert plan["jump"]["rows"] == {"count": 2, "first": 100, "last": 101}


def test_clap_results_are_reloaded_as_clap_results():
    with patch.object(AD, "get_db_rec", return_value=([], [], None, None)) as fetch:
        AD.plan_jump("2026-09-01T14:05", None, [{"field": "similarity"}], AD.VIEW_ALL, [], "laughter", 0.3, NOW)
    assert fetch.call_args.args[2:] == ("laughter", 0.3)


def test_while_parked_the_parked_range_is_what_is_loaded():
    with patch.object(AD, "get_db_rec") as fetch:
        plan = AD.plan_jump("2026-09-03T08:00", PARKED, [], AD.VIEW_ALL, [], None, 0.25, NOW)
    fetch.assert_not_called()
    assert plan["parked"] == {"range": PARKED["range"], "at": "2026-09-03 08:00:00"}


def test_without_a_moment_nothing_moves():
    assert AD.plan_jump("", None, [], AD.VIEW_ALL, [], None, 0.25, NOW) == {"note": "Pick a date and a time first."}


def test_back_to_latest_is_the_live_week_on_page_one():
    with patch.object(AD, "get_db_rec", return_value=(rows("2026-09-17 11:59:00"), [], None, None)) as fetch:
        plan = AD.plan_back_to_latest(AD.VIEW_ALL, [], 0.25, NOW)
    fetch.assert_called_once_with(*LIVE, None, 0.25)
    assert plan["parked"] is None and plan["query"] == "" and plan["note"] == ""
    assert plan["jump"]["first_page"] and plan["jump"]["rows"]["count"] == 1
    assert AD.show_back_to_latest(None) == {'display': 'none'}
    assert AD.show_back_to_latest(PARKED) == {}


# --- the automatic reload -------------------------------------------------

def refresh(trigger, parked):
    with patch.object(AD, "ctx", MagicMock(triggered_id=trigger)), \
         patch.object(AD, "get_sentinel_status_elements", return_value="status"), \
         patch.object(AD, "get_active_group_name", return_value="group"), \
         patch.object(AD, "room_filter_options", return_value=[]), \
         patch.object(AD, "get_db_rec", return_value=(rows("2026-09-01 14:04:00"), [], "a", "b")) as fetch:
        out = AD.refresh_page("/", 3, AD.VIEW_ALL, [], None, 0.25, False, parked)
    return out, fetch


def test_the_automatic_reload_leaves_a_parked_grid_alone():
    out, fetch = refresh("interval-component", PARKED)
    fetch.assert_not_called()
    assert out[0] is AD.no_update and out[-1] is AD.no_update
    assert out[10] == "status"                    # the sentinel list still refreshes


def test_the_automatic_reload_goes_on_while_live():
    out, fetch = refresh("interval-component", None)
    assert fetch.call_args.args[:2] == (None, None)       # the live week
    assert out[0] and out[-1] is AD.no_update


def test_a_view_change_while_parked_keeps_the_range_and_lands_again():
    out, fetch = refresh("event-view-filter", PARKED)
    assert fetch.call_args.args[:2] == tuple(PARKED["range"])
    assert out[-1]["at"] == PARKED["at"] and out[-1]["rows"]["count"] == 1


# --- Refresh and delete -----------------------------------------------------

def press_refresh(start, end):
    with patch.object(AD, "ctx", MagicMock(triggered_id="rec_database_refresh_button")), \
         patch.object(AD, "get_db_rec", return_value=(rows("2026-09-11 10:00:00"), [], None, None)) as fetch:
        out = AD.refresh_button_db(1, None, start, end, "", 0.25, AD.VIEW_ALL, [])
    return out, fetch


def test_refresh_with_days_picked_in_the_calendar_holds_them():
    out, fetch = press_refresh("2026-09-10", "2026-09-12")
    assert fetch.call_args.args[:2] == ("2026-09-10 00:00:00", "2026-09-12 23:59:59")
    assert out[7] == {"range": ["2026-09-10 00:00:00", "2026-09-12 23:59:59"], "at": None}
    assert out[5] is AD.no_update and out[6] is AD.no_update     # the picker keeps what was picked
    assert out[8]["first_page"]


def test_refresh_without_picking_shows_the_latest_and_the_picker_follows():
    out, fetch = press_refresh("2026-09-01 00:00:00", "2026-09-08 23:59:59")   # set by the page days ago
    live = AD.live_range()
    assert fetch.call_args.args[:2] == live
    assert out[7] is None and (out[5], out[6]) == live


def test_after_a_delete_the_grid_keeps_its_range_and_its_view():
    loaded = rows("2026-09-01 14:00:00") + [{"ID": 7, "Timestamp": "2026-09-01 14:01:00",
                                             "Class 1": "Dishes, pots, and pans",
                                             "transcription": "No speech detected (AST)"}]
    with patch.object(AD, "Ast_Sql_lite"), \
         patch.object(AD, "get_db_rec", return_value=(loaded, [], None, None)) as fetch:
        out = AD.delete_selected_rows(1, [{"ID": 3}], PARKED, AD.VIEW_SPEECH, [])
    assert fetch.call_args.args[:2] == tuple(PARKED["range"])
    assert [row["ID"] for row in out] == [100]


# --- layout -------------------------------------------------------------------

def test_the_bar_sits_above_the_grid_and_the_landed_row_is_marked():
    layout = AD.layout()
    for wanted in ('grid-jump-time', 'grid-jump-go', 'grid-back-live', 'grid-jump-note',
                   'grid-page-span', 'grid-parked', 'grid-jump'):
        assert find(layout, wanted) is not None, wanted
    grid = find(layout, 'audio_grid_table')
    assert grid.rowClassRules == {"sx-row-jumped": {"function": "sxIsJumpedRow(params)"}}
