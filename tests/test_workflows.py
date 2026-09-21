"""The three workflows, as configuration that can drift.

Sale runs hourly, rent runs weekly, and both write the same listings.csv; the
weekly backup reads what the two of them produced. Nothing in Python enforces
any of that - it lives in YAML files that were copied from one another and
will be edited separately. These are the checks that notice when they stop
agreeing: when sale wanders into the hours rent owns, or when the backup
starts before rent could have finished.
"""

from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"
SALE = WORKFLOWS / "scrape.yml"
# The second scrape workflow. It used to be the weekly rent pass; rent now
# rides the hourly area pass and this nightly city-wide one, so the tests that
# say "both scrapes" mean these two.
RENT = WORKFLOWS / "scrape-night.yml"


def load(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def inputs_of(spec):
    # PyYAML parses the bare `on:` key as the boolean True.
    return spec[True]["workflow_dispatch"]["inputs"]


def step(spec, name):
    return [s for s in spec["jobs"]["scrape"]["steps"] if s.get("name") == name][0]


def test_both_workflows_parse():
    assert load(SALE)["name"] and load(RENT)["name"]


def fires_at(spec, weekday, hour):
    """Whether any of a workflow's crons fires at this UTC weekday/hour.

    Only as clever as these schedules need: day-of-month and month always
    "*". Anything else would be a schedule nobody meant to write. The minute
    decides how many attempts an hour gets, not which hour they land in, so
    it plays no part here.
    """
    for entry in spec[True]["schedule"]:
        minute, hours, dom, month, dow = entry["cron"].split()
        assert (dom, month) == ("*", "*"), entry["cron"]
        del minute

        def matches(field, value):
            if field == "*":
                return True
            for part in field.split(","):
                if "-" in part:
                    low, high = (int(x) for x in part.split("-"))
                    if low <= value <= high:
                        return True
                elif int(part) == value:
                    return True
            return False

        if matches(dow, weekday) and matches(hours, hour):
            return True
    return False


def rent_windows():
    """Rent's Sunday windows, each as (first hour, last hour).

    Two, and they are not the same kind of thing. The first is the window
    sale is kept out of, so a pass there disturbs nothing. The second is the
    fallback for the Sunday the first one gets no attempt delivered at all -
    it lands in the middle of sale's hours on purpose, and leans on the guard
    and the shared concurrency group instead of on an empty schedule.
    """
    out = []
    for entry in load(RENT)[True]["schedule"]:
        hours = entry["cron"].split()[1]
        low, _, high = hours.partition("-")
        out.append((int(low), int(high or low)))
    return sorted(out)


def rent_window_start():
    """The first UTC hour of the Sunday window rent owns."""
    return rent_windows()[0][0]






def rent_guard_step(step_id):
    return [s for s in load(RENT)["jobs"]["guard"]["steps"]
            if s.get("id") == step_id][0]








def test_sale_runs_every_hour_outside_the_rent_window():
    sale = load(SALE)
    for hour in range(24):
        assert fires_at(sale, 3, hour), f"sale must run hourly on a weekday ({hour}:00)"


def scrape_group(spec):
    """The scrape's concurrency group, wherever it is declared.

    The sale workflow moved it onto the job so its guard can answer while a
    scrape is running; the rent workflow still declares it at the top.
    """
    if "concurrency" in spec:
        return spec["concurrency"]["group"]
    return spec["jobs"]["scrape"]["concurrency"]["group"]


def test_no_schedule_sits_on_the_top_of_the_hour():
    """GitHub delays and drops scheduled runs under load, and the top of the
    hour is the busiest slot there is - every cron anybody writes by hand
    lands on it. Asking for 24 sale runs a day at minute 0 got three on the
    first real day: 13:17, 18:01 and 21:58 UTC, the rest dropped outright.

    This is a mitigation, not a guarantee - the schedule event is best
    effort whatever minute it names - but minute 0 is the one slot known to
    be worst, and nothing here needs to run exactly on the hour.
    """
    for workflow in (SALE, RENT, REPORT):
        for entry in load(workflow)[True]["schedule"]:
            minute = entry["cron"].split()[0]
            assert minute != "0", f"{workflow.name}: {entry['cron']}"










def test_sale_resumes_the_same_sunday():
    """Skipping the window must not turn into skipping the day."""
    sale = load(SALE)
    assert any(fires_at(sale, 0, hour) for hour in range(5, 24))


def test_both_scrapes_collect_both_transactions():
    """They no longer split sale from rent.

    Rent had its own weekly workflow because it walked the whole city and
    needed four hours. The hourly pass now walks two boroughs, which is small
    enough that sale and rent fit in one run together - so rent is seen every
    hour instead of once a week, and the nightly city pass carries both as
    well."""
    for path in (SALE, RENT):
        default = inputs_of(load(path))["transactions"]["default"]
        assert set(default.split(",")) == {"prodej", "pronajem"}, (
            f"{path.name}: {default}")


def test_the_default_is_passed_through_to_run_py():
    """An input nobody reads is worse than no input: the default would look
    right in the UI while the run collected something else."""
    for path in (SALE, RENT):
        scrape = step(load(path), "Run scraper")
        assert "--transactions" in scrape["run"]
        assert "|| 'prodej,pronajem'" in scrape["env"]["TRANSACTIONS"], (
            f"{path.name}: {scrape['env']['TRANSACTIONS']}")


def test_both_workflows_share_one_concurrency_group():
    """They write the same listings.csv. Two runs merging into it at once
    would race and one would lose its work."""
    assert scrape_group(load(SALE)) == scrape_group(load(RENT))
    for spec in (load(SALE)["jobs"]["scrape"], load(RENT)["jobs"]["scrape"]):
        assert spec["concurrency"]["cancel-in-progress"] is False, (
            "queue behind a running scrape, never kill it mid-sweep"
        )


def test_the_two_confirmation_labels_are_different():
    """One shared label would let an open sale confirmation silence the
    weekly rent one - and rent is the run you hear from least often."""
    labels = {
        path: step(load(path), "Announce a successful run")["env"]["LABEL"]
        for path in (SALE, RENT)
    }
    assert len(set(labels.values())) == 2, labels


def test_only_sreality_is_ever_robots_overridden():
    """The override is the single place this project stops treating a
    robots.txt as binding, and a copied workflow is exactly how a second one
    would appear unnoticed."""
    for path in (SALE, RENT):
        override = step(load(path), "Run scraper")["env"]["SCRAPER_ROBOTS_OVERRIDE_HOSTS"]
        assert override == "www.sreality.cz"


def test_the_fetching_budget_leaves_room_for_the_rest_of_the_job():
    """The job also installs, tests, exports and commits. A budget that fills
    the timeout means a run gets killed mid-write."""
    for path in (SALE, RENT):
        spec = load(path)
        budget_s = int(inputs_of(spec)["max_seconds"]["default"])
        timeout_s = spec["jobs"]["scrape"]["timeout-minutes"] * 60
        assert budget_s < timeout_s - 600, (
            f"{path.name}: {budget_s}s of fetching in a {timeout_s}s job leaves too little"
        )


def test_the_hourly_run_fits_inside_an_hour():
    """A run that regularly exceeds its own interval pushes the next one back,
    and with a queueing concurrency group the schedule never recovers."""
    budget_s = int(inputs_of(load(SALE))["max_seconds"]["default"])
    assert budget_s <= 2700, f"{budget_s}s of fetching does not fit an hourly schedule"


def test_tests_run_before_any_scraping():
    """A scraper that writes to a committed dataset should not run at all if
    its own parsing logic is broken."""
    for path in (SALE, RENT):
        names = [s.get("name") for s in load(path)["jobs"]["scrape"]["steps"]]
        assert names.index("Run tests") < names.index("Run scraper")


# --- the weekly backup ------------------------------------------------------


BACKUP = WORKFLOWS.parent.parent / "deploy" / "backup.yml"  # runs in the data repo


def window(spec, weekday):
    """The UTC hours this workflow attempts on that weekday, first and last.

    Everything weekly now attempts across a window rather than once, because
    one shot at a scheduler that drops most of them is one shot at losing the
    week. So "when does it run" is a range, and the interesting end depends
    on the question: the first attempt for "has it started", the last for
    "could it still be running".
    """
    hours = [h for h in range(24) if fires_at(spec, weekday, h)]
    assert hours, f"nothing fires on weekday {weekday}"
    return hours[0], hours[-1]


def worst_finish(spec, job, weekday):
    """The latest a pass could still be running, in hours past midnight.

    An attempt lands at :45 of the window's final hour and then runs its
    whole timeout. Measuring from the window's START is how a backup at 05:30
    came to sit inside a rent pass that could run until 06:25.
    """
    return window(spec, weekday)[1] + 0.75 + spec["jobs"][job]["timeout-minutes"] / 60


def test_the_backup_only_attempts_on_monday_and_its_retry_day():
    """How MANY times it runs is the due job's business - it asks whether the
    week's tag already holds an archive. What the schedule owns is which days
    the attempts land on: Monday, and Tuesday when Monday produced nothing.

    Never Sunday. The archive is named for the week it holds, so taken on a
    Sunday it would hold a week with hours left in it and never revisit the
    tag.
    """
    days = {d for d in range(7) for h in range(24) if fires_at(load(BACKUP), d, h)}
    assert days == {1, 2}, days

def test_the_backup_runs_after_the_report_that_week():
    """The archive carries reports/, so one taken before the report holds
    every week's page except the one it is named after - and that is the week
    a restore of it would most want.
    """
    report_hours = [h for h in range(24) if fires_at(load(REPORT), 1, h)]
    backup_hours = [h for h in range(24) if fires_at(load(BACKUP), 1, h)]
    assert min(backup_hours) > max(report_hours), (report_hours, backup_hours)


def test_the_backup_does_not_queue_behind_the_scrapers():
    """It reads a checkout at one commit, so there is nothing to race over -
    and sharing the group would mean a long rent run could push the backup
    out of its slot entirely."""
    assert load(BACKUP)["concurrency"]["group"] != scrape_group(load(SALE))


def test_the_backup_never_writes_to_the_branch():
    spec = load(BACKUP)
    steps = spec["jobs"]["backup"]["steps"]
    body = " ".join(str(s.get("run", "")) for s in steps)
    assert "git push" not in body and "git commit" not in body, (
        "the backup publishes a release; it has no business rewriting data/"
    )
    assert set(spec["permissions"]) == {"contents"}


def test_the_backup_verifies_before_it_publishes():
    """Order matters: an archive that has not been restored is not a backup,
    and publishing first would mean a corrupt one still gets a release."""
    names = [s.get("name") for s in load(BACKUP)["jobs"]["backup"]["steps"]]
    assert names.index("Build and verify the archive") < \
        names.index("Publish the archive as a release")


def test_the_backup_tool_is_invoked_the_way_it_documents_itself():
    build = [s for s in load(BACKUP)["jobs"]["backup"]["steps"]
             if s.get("name") == "Build and verify the archive"][0]
    assert "tools.backup" in build["run"]
    assert "--github-output" in build["run"], "the release step reads those outputs"


# --- staying alive ----------------------------------------------------------


HEARTBEAT = WORKFLOWS / "heartbeat.yml"


def test_something_pushes_to_this_repository_often_enough():
    """GitHub disables scheduled workflows in a public repository after 60
    days without repository activity. Every commit this project produces goes
    to the private data repository, so without a deliberate push here the
    hourly scrape stops after two months - silently, with nothing red.

    The margin matters more than the exact day: a weekly beat leaves eight
    chances to notice before the window closes.
    """
    spec = load(HEARTBEAT)
    minute, hour, dom, month, dow = spec[True]["schedule"][0]["cron"].split()
    assert dom == "*" and month == "*", "a day-of-month schedule can skip months"
    assert dow != "*", "must be weekly or more often, not a monthly gamble"
    del minute, hour

    body = " ".join(str(s.get("run", "")) for s in spec["jobs"]["beat"]["steps"])
    assert "git push" in body, "a heartbeat that does not push is not activity"


def test_the_heartbeat_fails_loudly_if_it_has_nothing_to_push():
    """The failure that would otherwise be invisible: the step runs, commits
    nothing because the file did not change, exits 0, and the 60-day clock
    keeps ticking behind a green tick."""
    body = " ".join(str(s.get("run", ""))
                    for s in load(HEARTBEAT)["jobs"]["beat"]["steps"])
    assert "exit 1" in body, "an empty heartbeat must fail, not pass quietly"


def test_the_heartbeat_is_independent_of_the_scrape():
    """It must not live inside the workflow it keeps alive - that only beats
    when the thing is already running."""
    assert HEARTBEAT.exists()
    for path in (SALE, RENT):
        steps = load(path)["jobs"]["scrape"]["steps"]
        assert not any("STATUS.md" in str(s.get("run", "")) for s in steps)


# --- paths that must follow the dataset checkout ----------------------------


def test_every_data_path_points_into_the_dataset_checkout():
    """The dataset lives in store/, not beside the code. A path that was
    missed reads as an empty directory rather than as an error, so the
    symptom is silence: the first split rent run announced its success with
    "No run log was written" in place of every number, and was green doing it.
    """
    for path in (SALE, RENT):
        spec = load(path)
        steps = spec["jobs"]["scrape"]["steps"]
        body = "\n".join(str(s.get("run", "")) for s in steps)

        for stray in ('glob.glob("logs/', 'glob.glob("data/',
                      '--data-dir data', '--logs-dir logs'):
            assert stray not in body, (
                f"{path.name} still reads {stray!r}, which is empty under the "
                f"split layout and fails silently"
            )
        assert 'glob.glob("store/logs/' in body, \
            f"{path.name} does not read the run log at all"


def test_the_scraper_and_the_notification_read_the_same_logs():
    """A notification pointed at a different directory than the scraper wrote
    to is the exact failure above, and it cannot be seen from either half."""
    for path in (SALE, RENT):
        steps = load(path)["jobs"]["scrape"]["steps"]
        scrape = next(s for s in steps if s.get("name") == "Run scraper")
        announce = next(s for s in steps
                        if s.get("name") == "Announce a successful run")
        written = str(scrape.get("run", "")).split("--logs-dir")[1].split()[0]
        assert f'glob.glob("{written}/' in str(announce.get("run", "")), \
            f"{path.name}: the scraper writes {written} and the notification " \
            "reads somewhere else"


# --- the weekly report -------------------------------------------------------


REPORT = WORKFLOWS / "report.yml"


def test_the_report_is_weekly_not_hourly():
    """The CSV series underneath keeps updating every run - that is the
    record. The report is the page a person reads, and a page that changes
    hourly in a market that turns over in months trains you to ignore it."""
    spec = load(REPORT)
    entries = spec[True]["schedule"]
    assert len(entries) == 1
    minute, hour, dom, month, dow = entries[0]["cron"].split()
    assert dom == "*" and month == "*"
    assert dow != "*", "a weekly report needs a day of the week"
    del minute, hour


def test_the_scrapes_no_longer_write_the_report():
    """Two writers of one file, on two schedules, is how a weekly report
    quietly becomes an hourly one again."""
    for path in (SALE, RENT):
        body = " ".join(str(s.get("run", ""))
                        for s in load(path)["jobs"]["scrape"]["steps"])
        assert "tools.report" not in body, f"{path.name} still writes the report"


def test_the_scrapes_still_write_the_series():
    """The record must stay current even though the page does not."""
    for path in (SALE, RENT):
        body = " ".join(str(s.get("run", ""))
                        for s in load(path)["jobs"]["scrape"]["steps"])
        assert "tools.episodes" in body and "tools.market" in body


def test_the_report_rebuilds_the_whole_chain():
    """It must never be a report about whenever the CSVs happened to last be
    written - recomputing from listings.csv takes seconds."""
    body = " ".join(str(s.get("run", ""))
                    for s in load(REPORT)["jobs"]["report"]["steps"])
    for step in ("tools.episodes", "tools.market", "tools.report"):
        assert step in body, f"{step} missing from the weekly rebuild"


def test_the_report_waits_for_the_week_to_be_over():
    """The dataset's days are Prague days, so a report at midnight Prague
    would find Sunday still open and cover a week ending Saturday. 00:00 UTC
    is 01:00 or 02:00 there - past the boundary in both halves of the year.
    """
    days = {d for d in range(7) for h in range(24) if fires_at(load(REPORT), d, h)}
    assert days == {1, 2}, days
    hours = [h for h in range(24) if fires_at(load(REPORT), 1, h)]
    for offset in (1, 2):
        assert all((h + offset) % 24 >= 1 for h in hours), (offset, hours)


def test_the_report_uses_the_dataset_checkout():
    body = " ".join(str(s.get("run", ""))
                    for s in load(REPORT)["jobs"]["report"]["steps"])
    assert "--data-dir store/data" in body
    assert 'glob.glob("logs/' not in body


# --- the guard: four attempts an hour, one run ------------------------------


def guard_job():
    return load(SALE)["jobs"]["guard"]


def test_the_schedule_attempts_far_more_often_than_it_wants_to_run():
    """GitHub drops most schedule events, and no cron expression fixes that.
    Asking four times an hour and discarding three is the way to get one."""
    for entry in load(SALE)[True]["schedule"]:
        assert entry["cron"].split()[0] == "*/15", entry["cron"]


def test_the_guard_is_outside_the_group_it_guards():
    """Inside it, the guard would queue behind the running scrape and wake to
    find the coast clear - which is the pile-up it exists to prevent."""
    assert "concurrency" not in load(SALE), "must not be workflow-wide"
    assert "concurrency" not in guard_job()
    assert scrape_group(load(SALE)) == "scrape-data"


def test_the_scrape_only_runs_when_the_guard_says_so():
    scrape = load(SALE)["jobs"]["scrape"]
    assert scrape["needs"] == "guard"
    assert "needs.guard.outputs.go == 'true'" in scrape["if"]


def decide_step():
    return [s for s in guard_job()["steps"] if s.get("id") == "decide"][0]


def test_the_guard_logic_lives_in_a_script_that_can_be_tested():
    """Inline YAML shell cannot be tested, and this step decides whether the
    project collects anything at all. See tests/test_scrape_guard.py."""
    step = decide_step()
    assert "tools/scrape_guard.sh" in str(step["run"])
    assert (Path(__file__).resolve().parents[1]
            / "tools" / "scrape_guard.sh").exists()


def test_the_guard_checks_out_the_code_it_runs():
    uses = [s.get("uses", "") for s in guard_job()["steps"]]
    assert any(u.startswith("actions/checkout") for u in uses)


def test_the_decision_reaches_the_scrape_job():
    assert decide_step()["run"].strip().endswith('>> "$GITHUB_OUTPUT"')
    assert guard_job()["outputs"]["go"] == "${{ steps.decide.outputs.go }}"


def test_the_guard_keeps_the_hourly_floor():
    env = decide_step()["env"]
    assert 45 <= int(env["MIN_GAP_MINUTES"]) < 60, (
        "under an hour so the cadence does not drift later every run, but "
        "close enough to it that the portals still see one sweep an hour"
    )


def test_a_run_the_guard_stopped_does_not_count_as_a_run():
    """The killer bug in this design: a skipped run finishes in seconds and,
    counted as the last run, would hold the floor closed forever."""
    assert int(decide_step()["env"]["MIN_REAL_RUN_SECONDS"]) >= 120


def test_the_guard_is_told_what_triggered_the_run():
    """Without this the script cannot tell a person from the schedule and
    would ration the button press too."""
    assert "github.event_name" in decide_step()["env"]["EVENT_NAME"]


def test_the_schedule_path_can_be_exercised_on_demand():
    """The guard short-circuits for a hand-triggered run, so the button never
    reaches the branch that calls the API - the one that decides everything
    and can only fail in production. Waiting for GitHub to deliver a cron is
    not a test strategy when GitHub is the thing that is unreliable."""
    assert "as_schedule" in inputs_of(load(SALE))
    env = decide_step()["env"]["EVENT_NAME"]
    assert "inputs.as_schedule" in env and "'schedule'" in env


def test_the_guard_may_read_run_history():
    assert load(SALE)["permissions"]["actions"] == "read"


# --- the health check must be wired in, not just written --------------------


def health_step(workflow):
    steps = load(workflow)["jobs"]["scrape"]["steps"]
    found = [s for s in steps if "tools.health" in str(s.get("run", ""))]
    assert len(found) == 1, f"{workflow.name}: expected one health step"
    return found[0], steps


@pytest.mark.parametrize("workflow", [SALE, RENT])
def test_both_scrapes_check_their_own_health(workflow):
    """A check nothing runs is a file, not a check."""
    step, _ = health_step(workflow)
    assert "--data-dir store/data" in step["run"]
    assert "--logs-dir store/logs" in step["run"], (
        "the dataset is checked out into store/; reading logs/ finds an "
        "empty directory and reports nothing, green"
    )


@pytest.mark.parametrize("workflow", [SALE, RENT])
def test_the_health_check_runs_even_after_a_failed_scrape(workflow):
    """A failed run still knows how long the silence before it was, and that
    is exactly when you want to hear about it."""
    step, _ = health_step(workflow)
    assert "always()" in step["if"]


@pytest.mark.parametrize("workflow", [SALE, RENT])
def test_the_health_check_comes_before_the_success_announcement(workflow):
    """Otherwise a run announces itself as fine and only then discovers it
    is not, and the email you get says the wrong thing."""
    _, steps = health_step(workflow)
    names = [str(s.get("name") or s.get("run", "")) for s in steps]
    health_at = next(i for i, s in enumerate(steps)
                     if "tools.health" in str(s.get("run", "")))
    announce_at = next(i for i, n in enumerate(names)
                       if n.startswith("Announce a successful run"))
    assert health_at < announce_at




def test_probe_entry_points_come_after_everything_they_call():
    """A probe that appends a helper after `if __name__ == "__main__"` runs
    main() before the helper exists, and dies with NameError halfway through
    a run that has already spent its requests. Happened once."""
    import ast
    from pathlib import Path

    tools = Path(__file__).resolve().parent.parent / "tools"
    for path in sorted(tools.glob("probe_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        guards = [i for i, node in enumerate(tree.body)
                  if isinstance(node, ast.If) and ast.unparse(node.test).startswith("__name__")]
        if not guards:
            continue
        assert guards[0] == len(tree.body) - 1, (
            f"{path.name}: code follows the __main__ guard, so it is not "
            "defined when main() runs"
        )


# --- the nightly city-wide pass ---------------------------------------------

NIGHT = WORKFLOWS / "scrape-night.yml"


def test_the_nightly_pass_walks_the_whole_city():
    """Its whole reason to exist. Narrowed, it would leave the rest of Prague
    unseen and - worse - absence marking would run off a walk that never
    covered the population it judges."""
    step = [s for s in load(NIGHT)["jobs"]["scrape"]["steps"]
            if s.get("id") == "scrape"][0]
    assert "--scope city" in step["run"], step["run"]


def test_the_nightly_pass_collects_both_transactions():
    """Rent has no weekly workflow any more; it rides here and in the hourly
    area pass."""
    default = inputs_of(load(NIGHT))["transactions"]["default"]
    assert set(default.split(",")) == {"prodej", "pronajem"}


def test_the_nightly_pass_does_not_collect_sreality():
    """sreality's robots.txt disallows everything, so the traffic sent there
    stays as small as the question allows. This pass collects iDNES and
    bezrealitky city-wide and does not collect sreality at all.

    It does now READ sreality's index once, as a coordinate donor - see the
    test below. That is a deliberate change from the original rule that
    nothing walks it city-wide, and it is a change of about 25 requests a day
    against the ~840 the hourly area passes already make, on the cheapest
    path the API offers. What has not changed is that no sreality advert is
    stored by this pass."""
    default = inputs_of(load(NIGHT))["sources"]["default"]
    assert "sreality" not in default, default


def test_the_nightly_pass_borrows_coordinates_and_then_uses_them():
    """iDNES publishes no coordinates and is 64% of the dataset. Borrowing
    one and not filling the house number would leave the work half done, and
    the workflow green either way."""
    steps = load(NIGHT)["jobs"]["scrape"]["steps"]
    runs = [s.get("run", "") for s in steps]
    joined = " ".join(runs)
    assert "tools.lend_gps_from_sreality" in joined
    assert "tools.backfill_cislo" in joined

    lend_at = next(i for i, r in enumerate(runs)
                   if "lend_gps_from_sreality" in r)
    fill_at = next(i for i, r in enumerate(runs) if "backfill_cislo" in r)
    scrape_at = next(i for i, s in enumerate(steps) if s.get("id") == "scrape")
    assert scrape_at < lend_at < fill_at, (
        "the order has to be collect, then borrow, then number: reversed, a "
        "listing found tonight waits a day for its coordinate and another "
        "for its number")

    # Both tools default to a dry run. Without --apply each prints a report,
    # writes nothing, exits zero, and the workflow stays green while doing
    # precisely nothing - which is the most expensive kind of success.
    assert "--apply" in runs[lend_at], runs[lend_at]
    assert "--apply" in runs[fill_at], runs[fill_at]


def test_borrowing_coordinates_overrides_robots_for_the_same_one_host():
    """The step reaches sreality, so it needs the project's single override -
    and must not widen it."""
    steps = load(NIGHT)["jobs"]["scrape"]["steps"]
    step = next(s for s in steps
                if "lend_gps_from_sreality" in s.get("run", ""))
    hosts = (step.get("env") or {}).get("SCRAPER_ROBOTS_OVERRIDE_HOSTS")
    assert hosts == "www.sreality.cz", (
        f"the donor step's robots override is {hosts!r}")


def test_a_nightly_pass_finishes_before_the_morning():
    """An attempt lands at :45 of the window's last hour and then runs its
    whole budget. Measured the pass is about an hour; the point of the bound
    is that a late start still clears the morning."""
    night = load(NIGHT)
    hours = [h for h in range(24) if fires_at(night, 3, h)]
    budget_h = int(inputs_of(night)["max_seconds"]["default"]) / 3600
    worst_finish = hours[-1] + 0.75 + budget_h
    assert worst_finish <= 5, f"a late pass runs to {worst_finish:.2f}h UTC"


def test_the_nightly_window_is_night_in_both_halves_of_the_year():
    """GitHub cron is UTC-only. Prague is +01:00 in winter and +02:00 in
    summer, so a window that reads as night in one can be morning in the
    other."""
    hours = [h for h in range(24) if fires_at(load(NIGHT), 3, h)]
    for offset in (1, 2):
        local = [(h + offset) % 24 for h in hours]
        assert all(0 <= h <= 5 for h in local), (offset, local)


def test_the_nightly_pass_runs_every_day():
    days = {d for d in range(7) for h in range(24) if fires_at(load(NIGHT), d, h)}
    assert days == set(range(7)), days


def test_the_nightly_guard_asks_the_run_log():
    """What makes it daily. Measuring the Actions history instead would count
    a guard-stopped attempt as a pass, the mistake that cost the rent pass a
    week."""
    step = [s for s in load(NIGHT)["jobs"]["guard"]["steps"]
            if s.get("id") == "week"][0]
    assert "tools.night_due" in step["run"]


# --- the monthly address index ----------------------------------------------

RUIAN = WORKFLOWS / "build-ruian-index.yml"


def _ruian_steps():
    return load(RUIAN)["jobs"]["build"]["steps"]


def test_the_index_build_is_the_one_place_the_krovak_tests_run():
    """tests/test_krovak.py skips itself when pyproj is absent, which is
    honest in the hourly scrape - that job has no use for a projection
    library and installs requirements-dev.txt without one.

    It stops being honest if there is nowhere left that does install it. This
    job is that place, and the assertion is what keeps the skip from quietly
    becoming permanent everywhere."""
    steps = _ruian_steps()
    installs = " ".join(s.get("run", "") for s in steps)
    assert "requirements-ruian.txt" in installs, (
        "nothing installs pyproj any more, so the coordinate conversion is "
        "now untested everywhere")
    assert any("pytest" in s.get("run", "") for s in steps), (
        "pyproj is installed but the tests are not run, so installing it "
        "proves nothing")


def test_pyproj_is_not_in_the_hourly_scrape_s_dependencies():
    """The reason the index build exists as a separate job. requirements.txt
    is installed on every hourly run; a projection library there would be
    downloaded twenty-four times a day to be used never."""
    root = Path(__file__).resolve().parent.parent
    for name in ("requirements.txt", "requirements-dev.txt"):
        text = (root / name).read_text(encoding="utf-8")
        assert "pyproj" not in text, f"{name} pulls in pyproj"


def test_the_index_build_is_guarded_by_the_index_and_not_by_the_clock():
    """Same rule as every other guard here: ask the artifact. A schedule-based
    check answers "did the workflow fire", which came apart from "is the
    output there" every single time it was trusted."""
    guard = load(RUIAN)["jobs"]["guard"]["steps"]
    decide = [s for s in guard if s.get("id") == "decide"][0]
    assert "tools.ruian_due" in decide["run"]
    assert "ruian_praha.csv.gz" in decide["run"]
    assert load(RUIAN)["jobs"]["build"]["if"] == "needs.guard.outputs.go == 'true'"


def test_the_index_build_shares_the_data_lock():
    """It rewrites every row of listings.csv. Outside the lock it would race
    an hourly scrape doing the same, and one of the two would lose its work."""
    assert load(RUIAN)["jobs"]["build"]["concurrency"]["group"] == "scrape-data"


def test_the_index_is_applied_and_not_merely_built():
    """A built index that nothing reads leaves the column empty and the
    workflow green, which is the most expensive kind of success."""
    runs = " ".join(s.get("run", "") for s in _ruian_steps())
    assert "tools.build_ruian_index" in runs
    assert "tools.backfill_cislo" in runs
    assert "--apply" in runs, "the backfill would run as a dry run and write nothing"


def test_the_index_build_commits_what_it_produced():
    steps = _ruian_steps()
    assert any("commit_data.sh" in s.get("run", "") for s in steps), (
        "the index would be rebuilt every month and thrown away every month")


# --- the hourly pass, now scoped to the watched boroughs --------------------

def test_the_hourly_pass_walks_the_watched_boroughs_not_the_city():
    """The whole point of the change: the hourly pass used to pull all of
    Prague and throw ~92% of it away - 8,341 rows fetched, 7,658 discarded,
    every hour. Two boroughs is what the watched belt actually sits in."""
    scrape = step(load(SALE), "Run scraper")
    assert "--scope area" in scrape["run"], scrape["run"]


def test_the_nightly_pass_is_still_the_one_that_walks_everything():
    """Area scope is only safe because something else still covers the city.
    Absence is decided from the nightly pass; if both narrowed, every listing
    outside two boroughs would look gone."""
    assert "--scope city" in step(load(RENT), "Run scraper")["run"]


def test_the_hourly_pass_runs_every_hour_all_week():
    """The Sunday hole existed solely to leave the weekly rent window clear.
    There is no weekly rent window any more, so a hole there would just be
    five hours a week of nothing collected."""
    night = load(SALE)
    for weekday in range(7):
        hours = [h for h in range(24) if fires_at(night, weekday, h)]
        assert len(hours) == 24, (
            f"weekday {weekday} is only covered for {len(hours)} hours")


def test_nothing_schedules_a_weekly_rent_pass_any_more():
    """Left behind, it would walk the whole city for rent once a week on top
    of the hourly pass that already has it - the same adverts, twice."""
    assert not (WORKFLOWS / "scrape-rent.yml").exists()
    for path in sorted(WORKFLOWS.glob("*.yml")):
        text = path.read_text(encoding="utf-8")
        assert "scrape-rent" not in text, f"{path.name} still refers to it"


def test_the_rent_guard_is_gone_with_the_workflow_it_guarded():
    """tools/rent_due.py answered "is this week's rent already collected".
    With rent collected hourly the question has no meaning, and a guard that
    still answers a meaningless question is a trap for the next reader."""
    root = Path(__file__).resolve().parent.parent
    assert not (root / "tools" / "rent_due.py").exists()
