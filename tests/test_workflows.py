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
RENT = WORKFLOWS / "scrape-rent.yml"


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


def test_rent_attempts_only_ever_land_on_sunday_night():
    """The cron does not decide how OFTEN rent runs - tools/rent_due.py does,
    from the dataset. What the cron decides is WHERE the attempts land and
    how many there are, and both matter for a different reason: these hours
    are the ones the external waker sleeps through, so GitHub's own scheduler
    is all rent has, and it drops most of what it is asked for. Every extra
    attempt is another chance at the week; the due check makes the extras
    free."""
    rent = load(RENT)
    days = {d for d in range(7) for h in range(24) if fires_at(rent, d, h)}
    assert days == {0}, f"rent must only attempt on Sunday, got {days}"
    assert rent_windows()[0] == (0, 3), rent_windows()


def test_a_late_rent_start_does_not_eat_sunday_morning():
    """Attempts land across a window, so the pass can start at the END of it
    and still run its full budget. With a four-hour window that put the worst
    case at 07:45 - sale resumes at 05:00, and the guard would have stood it
    down for nearly three hours. More attempts is better for rent, but not at
    that price."""
    rent = load(RENT)
    budget_h = int(inputs_of(rent)["max_seconds"]["default"]) / 3600
    # Last attempt of the window is at :45 of its final hour.
    worst_finish = rent_windows()[0][1] + 0.75 + budget_h
    sale_resumes = min(h for h in range(24) if fires_at(load(SALE), 0, h))
    assert worst_finish - sale_resumes <= 1, (
        f"a pass starting at the end of the window runs to {worst_finish:.2f}h, "
        f"and sale resumes at {sale_resumes}:00"
    )


def rent_guard_step(step_id):
    return [s for s in load(RENT)["jobs"]["guard"]["steps"]
            if s.get("id") == step_id][0]


def test_rent_may_only_start_one_pass_a_week():
    """And it asks the dataset, not the run history.

    This was a six-day floor on the Actions run history, and it lost the first
    Sunday it was asked to work: it measured from a manual test run on the
    Wednesday - inside the floor - and stopped the pass in ten seconds. The
    run it deferred to had left no rent in the dataset at all.
    """
    assert "tools.rent_due" in rent_guard_step("week")["run"], (
        "what makes the pass weekly is whether the dataset already holds "
        "this week's rent - nothing else answers that"
    )
    go = load(RENT)["jobs"]["guard"]["outputs"]["go"]
    assert "steps.week.outputs.go" in go and "steps.decide.outputs.go" in go, (
        "both questions gate the pass; either one alone lets it through"
    )


def test_the_rent_floor_is_a_retry_brake_and_not_the_weekly_schedule():
    """A floor long enough to span a week is the bug described above."""
    env = rent_guard_step("decide")["env"]
    assert env["MEASURE_WORKFLOW"] == "scrape-rent.yml", (
        "measured against the hourly sale sweep, rent would never run"
    )
    floor_minutes = int(env["MIN_GAP_MINUTES"])
    assert 45 <= floor_minutes <= 120, (
        "long enough that a pass which died before committing is not retried "
        "at every attempt for the rest of the window, short enough that it "
        "cannot silently become the thing that decides the week"
    )


def test_the_rent_guard_can_read_the_dataset_it_asks_about():
    """The due check needs listings.csv present, and only that."""
    checkout = [s for s in load(RENT)["jobs"]["guard"]["steps"]
                if "checkout" in str(s.get("uses", "")) and "with" in s
                and s["with"].get("path") == "store"][0]["with"]
    assert checkout["sparse-checkout"] == "data/listings.csv"
    assert checkout["sparse-checkout-cone-mode"] is False, (
        "cone mode matches directories, not a single file"
    )
    assert "DATA_REPO_KEY" in checkout["ssh-key"]


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


def test_sale_stays_out_of_the_window_rent_owns():
    """Kept out rather than queued: the two share a concurrency group, so
    queueing would pile four hours of sale runs into a heap that all fire at
    once the moment rent finishes, collecting the same hour four times.

    This is about rent's FIRST window, the one that exists so a normal week's
    pass disturbs nothing. The fallback window is the opposite trade and has
    its own test below.
    """
    sale, rent = load(SALE), load(RENT)
    rent_budget_hours = int(inputs_of(rent)["max_seconds"]["default"]) / 3600
    rent_start = rent_window_start()

    for offset in range(int(rent_budget_hours) + 1):
        hour = (rent_start + offset) % 24
        assert not fires_at(sale, 0, hour), (
            f"a sale run is scheduled in the {hour}:00 UTC hour on Sunday, "
            "inside rent's window"
        )


def test_rent_has_a_fallback_window_for_the_sunday_the_first_one_is_dropped():
    """On 2026-09-20 GitHub delivered one of sixteen attempts into rent's
    first window, and none of eight into the backup's. A weekly pass whose
    only window comes up empty loses the week, so there is a second one."""
    windows = rent_windows()
    assert len(windows) == 2, f"expected a fallback window, got {windows}"


def test_the_fallback_cannot_collide_with_a_pass_from_the_first_window():
    """Both windows feed one concurrency group. An attempt arriving while the
    first window's pass still holds it is stopped by the guard as busy - so
    the fallback would be spent on nothing. It has to open after the first
    window's worst case is over."""
    rent = load(RENT)
    first, fallback = rent_windows()
    worst_finish = first[1] + 0.75 + rent["jobs"]["scrape"]["timeout-minutes"] / 60
    assert fallback[0] > worst_finish, (
        f"the fallback opens at {fallback[0]}:00 but a pass from the first "
        f"window can still be running at {worst_finish:.2f}h"
    )


def test_the_fallback_leaves_the_rest_of_sunday_alone():
    """It is a hedge, not a second schedule: a handful of attempts, and over
    before the evening."""
    _, fallback = rent_windows()
    assert fallback[1] - fallback[0] <= 3, "a fallback, not a whole afternoon"
    budget_h = int(inputs_of(load(RENT))["max_seconds"]["default"]) / 3600
    assert fallback[1] + 0.75 + budget_h < 20, "must finish well inside Sunday"


def test_sale_resumes_the_same_sunday():
    """Skipping the window must not turn into skipping the day."""
    sale = load(SALE)
    assert any(fires_at(sale, 0, hour) for hour in range(5, 24))


def test_each_workflow_collects_its_own_half():
    assert inputs_of(load(SALE))["transactions"]["default"] == "prodej"
    assert inputs_of(load(RENT))["transactions"]["default"] == "pronajem"


def test_the_default_is_passed_through_to_run_py():
    """An input nobody reads is worse than no input: the default would look
    right in the UI while the run collected something else."""
    for path, expected in ((SALE, "prodej"), (RENT, "pronajem")):
        scrape = step(load(path), "Run scraper")
        assert "--transactions" in scrape["run"]
        assert f"|| '{expected}'" in scrape["env"]["TRANSACTIONS"]


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


def test_the_backup_only_ever_attempts_on_sunday():
    """How MANY times it runs is the due job's business now - it asks the
    releases whether the week is already archived. What the schedule still
    owns is which day the attempts land on."""
    days = {d for d in range(7) for h in range(24) if fires_at(load(BACKUP), d, h)}
    assert days == {0}, days

def test_the_backup_waits_for_the_rent_pass_to_be_over():
    """Derived from rent's own budget rather than asserted as a magic hour:
    if someone lengthens the rent window, this fails instead of quietly
    archiving a week that is missing its rent data."""
    rent_latest_end = worst_finish(load(RENT), "scrape", 0) * 60
    backup_starts = window(load(BACKUP), 0)[0] * 60
    assert backup_starts > rent_latest_end, (
        "the backup can start before the rent run's worst case has finished, "
        "so a week's rent data could miss the archive"
    )


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


def test_the_report_runs_after_the_rent_pass_and_its_backup():
    """Sunday's rent run and the backup that follows it are what make the
    week complete; a report before them is a report about six days."""
    days = {d for d in range(7) for h in range(24) if fires_at(load(REPORT), d, h)}
    assert days == {1}, f"Monday, so Sunday's rent pass is already in it: {days}"
    # The backup is Sunday and the report Monday, so the only way they could
    # collide is a backup running past midnight.
    assert worst_finish(load(BACKUP), "backup", 0) < 24, (
        "the Sunday backup could still be running on Monday morning"
    )


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


def test_the_rent_detail_cap_fits_inside_the_fetching_budget():
    """Both are numbers in the same YAML and neither knows about the other.

    Measured on the first real pass (2026-09-20): 4254s elapsed of which
    ~1500 were detail fetches at one a second, so the sweep itself is about
    2750s. Raise the cap without raising the budget and the pass gets cut
    short - and a rent pass cut short waits a week for the rest.
    """
    rent_inputs = inputs_of(load(RENT))
    cap = int(rent_inputs["max_new_details"]["default"])
    budget = int(rent_inputs["max_seconds"]["default"])
    sweep_seconds = 2750
    assert sweep_seconds + cap <= budget * 0.85, (
        f"{cap} details plus a {sweep_seconds}s sweep leaves nothing spare "
        f"in a {budget}s budget"
    )
