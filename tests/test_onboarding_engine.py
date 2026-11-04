import json
import stat

import pytest

from onboarding.engine import BLOCKED, DONE, NOT_NEEDED, READY, SKIPPED, EngineError
from onboarding.registry import MissingInput
from onboarding.session import Session
from tests.onboarding_fakes import Toy


@pytest.fixture
def toy(tmp_path):
    return Toy(tmp_path)


def answer(toy, **values):
    toy.session.answers.update(values)


async def test_the_first_unsatisfied_step_is_next_and_what_depends_on_it_is_blocked(toy):
    states = await toy.states()
    assert states["base"] == READY
    assert states["net"] == BLOCKED
    assert states["fast-only"] == BLOCKED
    items = await toy.ob.evaluate()
    assert toy.ob.next_item(items).node.key == "base"
    assert next(i for i in items if i.node.key == "net").blocked_by == ("base",)


async def test_a_step_is_done_only_when_its_probes_say_so(toy):
    answer(toy, who="ann", nick="", color="red")
    toy.flags["base"] = True
    assert (await toy.states())["base"] == DONE
    assert (await toy.states())["net"] == READY
    toy.flags["base"] = False  # it regressed in the real world
    assert (await toy.states())["base"] == READY


async def test_probe_results_are_cached_until_something_changes(toy):
    answer(toy, who="ann", nick="", color="red")
    toy.flags["base"] = True
    assert (await toy.states())["base"] == DONE
    toy.flags["base"] = False
    assert (await toy.states(fresh=False))["base"] == DONE  # still the cached answer
    assert (await toy.states(fresh=True))["base"] == READY


async def test_a_failing_probe_is_unknown_not_a_crash(toy):
    answer(toy, who="ann", nick="", color="red")
    toy.flags["base"] = True
    status = await toy.ob.detect(toy.node("fragile"), fresh=True)
    assert status.state == "unknown" and "boom failed" in status.detail and "kaput" in status.detail


async def test_pending_inputs_are_required_ones_and_optional_ones_not_yet_asked(toy):
    base = toy.node("base")
    assert toy.ob.pending(base) == ["who", "nick", "color"]
    assert toy.ob.missing(base) == ["who", "color"]  # a default is confirmed once; nick may stay empty
    toy.console.answers[:] = ["Ann", "ann", "", ""]  # the first fails the pattern; nick empty; colour keeps its default
    await toy.ob.ask_pending(base)
    assert "doesn't look right" in toy.console.text
    assert toy.session.answers == {"who": "ann", "nick": "", "color": "red"}
    assert toy.ob.pending(base) == []


async def test_choices_are_validated(toy):
    toy.console.answers[:] = ["green", "blue"]
    await toy.ob.ask(toy.node("base"), "color")
    assert "choose one of: red, blue" in toy.console.text
    assert toy.session.answers["color"] == "blue"


async def test_secrets_are_asked_hidden_and_never_land_in_the_session(tmp_path):
    toy = Toy(tmp_path, answers=["hunter2-secret"])
    net = toy.node("net")
    result = await toy.ob.ask(net, "key")
    assert result == "set (hidden)"
    assert toy.console.prompts[-1][1] is True  # hidden input
    assert toy.secrets.get("key") == "hunter2-secret"
    assert "key" not in toy.session.answers
    assert "key" not in toy.ob.values(net)
    assert "hunter2-secret" not in json.dumps(toy.session.answers)


async def test_generated_secrets_are_never_asked(toy):
    net = toy.node("net")
    assert "auto" not in toy.ob.pending(net)
    toy.ob.fill_generated(net)
    assert toy.secrets.has("auto") and not toy.console.prompts
    assert toy.secrets.get("auto") not in toy.secrets.redact("nothing here")  # but it is scrubbed where it appears
    assert "<hidden:auto>" in toy.secrets.redact(f"x {toy.secrets.get('auto')} y")


async def test_run_shows_what_it_will_do_and_runs_after_a_yes(tmp_path):
    toy = Toy(tmp_path, answers=["y"])
    answer(toy, who="ann", nick="", color="red")
    outcome = await toy.ob.run(toy.node("base"))
    assert outcome.ran and outcome.ok and outcome.status.done
    assert toy.flags["base"] is True
    assert "raise base" in toy.console.text  # the preview, before the question
    assert toy.session.log[-1]["event"] == "ran"


async def test_a_no_changes_nothing(tmp_path):
    toy = Toy(tmp_path, answers=["n"])
    answer(toy, who="ann", nick="", color="red")
    outcome = await toy.ob.run(toy.node("base"))
    assert not outcome.ran and "declined" in outcome.message
    assert "base" not in toy.flags
    assert toy.session.log[-1]["event"] == "declined"


async def test_a_failed_action_is_reported_and_the_step_stays_not_done(tmp_path):
    toy = Toy(tmp_path, answers=["y"])
    answer(toy, who="ann", nick="", color="red")
    toy.flags["base"] = True
    outcome = await toy.ob.run(toy.node("bad"))
    assert outcome.ran and not outcome.ok and outcome.message == "it broke"
    assert outcome.status.state == "missing"


async def test_run_refuses_a_blocked_step_and_one_with_inputs_missing(tmp_path):
    toy = Toy(tmp_path, answers=["y", "y"])
    blocked = await toy.ob.run(toy.node("net"))
    assert not blocked.ran and "Not now" in blocked.message
    needs_inputs = await toy.ob.run(toy.node("base"))
    assert not needs_inputs.ran and "who" in needs_inputs.message
    assert toy.flags == {} and len(toy.console.answers) == 2  # nothing was even asked


async def test_an_already_done_step_is_not_run_again(tmp_path):
    toy = Toy(tmp_path, answers=["y"])
    answer(toy, who="ann", nick="", color="red")
    toy.flags["base"] = True
    outcome = await toy.ob.run(toy.node("base"))
    assert not outcome.ran and outcome.ok and outcome.message == "Already done."
    assert len(toy.console.answers) == 1  # no confirmation was asked


async def test_a_step_that_does_not_apply_counts_as_satisfied(toy):
    answer(toy, who="ann", nick="", color="red", mode="slow")
    toy.flags["base"] = True
    states = await toy.states()
    assert states["fast-only"] == NOT_NEEDED
    answer(toy, mode="fast")
    assert (await toy.states())["fast-only"] == READY


async def test_optional_steps_can_be_skipped_required_ones_cannot(tmp_path):
    toy = Toy(tmp_path, answers=["y"])
    answer(toy, who="ann", nick="", color="red")
    toy.flags["base"] = True
    assert await toy.ob.skip(toy.node("extra")) == "skipped"
    assert (await toy.states())["extra"] == SKIPPED
    with pytest.raises(EngineError, match="required"):
        await toy.ob.skip(toy.node("net"))


async def test_a_step_nothing_can_detect_waits_for_the_operator(tmp_path):
    toy = Toy(tmp_path, answers=["n", "y"])
    node = toy.node("checked")
    assert (await toy.ob.detect(node)).detail == "waiting for your confirmation"
    assert "not done yet" in await toy.ob.acknowledge(node)
    assert node.key not in toy.session.acks
    assert await toy.ob.acknowledge(node) == "recorded"
    assert (await toy.ob.detect(node, fresh=True)).done
    with pytest.raises(EngineError):
        await toy.ob.acknowledge(toy.node("base"))  # that one is confirmed by detection


# Targets


async def add_site(toy, name, url="https://a.example"):
    toy.console.answers[:] = [name, url]
    return await toy.ob.add_target("site")


async def test_a_collect_step_is_done_when_enough_targets_exist(toy):
    assert (await toy.ob.detect(toy.node("sites"))).detail == "add 1 more site"
    await add_site(toy, "blog")
    assert (await toy.ob.detect(toy.node("sites"), fresh=True)).done


async def test_steps_repeat_per_target_and_wait_for_their_own_instance(toy):
    await add_site(toy, "blog", "https://blog.example")
    await add_site(toy, "shop", "https://shop.example")
    keys = [n.key for n in toy.ob.nodes()]
    assert keys.index("site-up@blog") < keys.index("site-up@shop") < keys.index("checked")
    assert toy.ob.title(toy.node("site-up@shop")) == "Site https://shop.example"
    toy.flags["up-blog"] = True
    answer(toy, who="ann", nick="", color="red")
    toy.flags.update(base=True, net=True)
    toy.secrets.set("key", "k" * 8, persist=False)
    states = await toy.states()
    assert states["site-up@blog"] == DONE and states["site-up@shop"] == READY


async def test_a_step_that_needs_a_per_target_step_waits_for_every_instance(toy):
    await add_site(toy, "blog", "https://blog.example")
    await add_site(toy, "shop", "https://shop.example")
    answer(toy, who="ann", nick="", color="red")
    toy.flags.update(base=True, net=True, **{"up-blog": True})
    toy.secrets.set("key", "k" * 8, persist=False)
    items = {i.node.key: i for i in await toy.ob.evaluate(True)}
    assert items["checked"].state == BLOCKED
    assert items["checked"].blocked_by == ("site-up@shop",)
    toy.flags["up-shop"] = True
    assert (await toy.states())["checked"] == READY


async def test_targets_have_unique_valid_names_and_a_limit(toy):
    await add_site(toy, "blog")
    toy.console.answers[:] = ["Bad Name", "blog", "shop", "https://s.example"]
    target = await toy.ob.add_target("site")
    assert target.name == "shop"
    assert "already used" in toy.console.text and "short lowercase name" in toy.console.text
    with pytest.raises(EngineError, match="at most 2"):
        await toy.ob.add_target("site")
    with pytest.raises(EngineError, match="no target kind"):
        await toy.ob.add_target("nope")


async def test_running_a_collect_step_adds_a_target(toy):
    toy.console.answers[:] = ["blog", "https://blog.example"]
    outcome = await toy.ob.run(toy.node("sites"))
    assert outcome.ran and outcome.status.done and outcome.message == "added site blog"


async def test_resolving_a_step_by_name(toy):
    await add_site(toy, "blog")
    assert toy.ob.resolve("site-up").key == "site-up@blog"  # only one instance
    await add_site(toy, "shop", "https://s.example")
    with pytest.raises(EngineError, match="site-up@blog, site-up@shop"):
        toy.ob.resolve("site-up")
    with pytest.raises(EngineError, match="no step"):
        toy.ob.resolve("nonsense")


async def test_a_per_target_step_without_targets_says_to_add_one(tmp_path):
    toy = Toy(tmp_path)
    with pytest.raises(EngineError, match="repeats per site; add one first"):
        toy.ob.resolve("site-up")


# Persistence


async def test_progress_resumes_from_the_session_file(tmp_path):
    state = tmp_path / "state"
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    first = Toy(tmp_path / "a", state=state)
    first.session.answers.update(who="ann", nick="", color="red")
    first.session.acks.add("checked")
    first.session.skipped.add("extra")
    await add_site(first, "blog")
    first.secrets.generate("auto")
    first.session.save()

    second = Toy(tmp_path / "b", state=state)
    assert second.session.answers["who"] == "ann"
    assert second.session.targets == {"site": {"blog": {"url": "https://a.example"}}}
    assert second.session.acks == {"checked"} and second.session.skipped == {"extra"}
    assert second.secrets.has("auto")  # generated secrets persist; the GitHub-token kind does not


async def test_the_session_and_secret_files_are_private(tmp_path):
    state = tmp_path / "state"
    toy = Toy(tmp_path, state=state)
    toy.ob.fill_generated(toy.node("net"))
    toy.session.save()
    for name in ("session.json", "secrets.json"):
        assert stat.S_IMODE((state / name).stat().st_mode) == 0o600
    assert stat.S_IMODE(state.stat().st_mode) == 0o700


def test_a_damaged_or_foreign_session_is_refused_not_overwritten(tmp_path):
    from onboarding.session import SessionError

    path = tmp_path / "session.json"
    path.write_text("{not json")
    with pytest.raises(SessionError, match="damaged"):
        Session.load(path, "toy")
    path.write_text(json.dumps({"playbook": "other"}))
    with pytest.raises(SessionError, match="belongs to"):
        Session.load(path, "toy")


async def test_complete_when_everything_is_satisfied_and_the_closing_text_is_filled_in(tmp_path):
    toy = Toy(tmp_path)
    answer(toy, who="ann")
    assert toy.ob.finish_text() == "Done, ann"
    assert not toy.ob.complete(await toy.ob.evaluate())


# Secrets that are only needed to run a step


async def test_a_finished_step_stays_finished_in_a_new_session_even_without_its_secret(toy):
    answer(toy, who="ann", nick="", color="red")
    toy.flags.update(base=True, net=True)
    assert not toy.secrets.has("key")  # typed in an earlier session, and never written to disk
    assert (await toy.states())["net"] == DONE  # the probe needs no key, so the step is done


async def test_running_a_step_still_needs_its_secret(tmp_path):
    toy = Toy(tmp_path)
    answer(toy, who="ann", nick="", color="red")
    toy.flags["base"] = True
    outcome = await toy.ob.run(toy.node("net"))
    assert not outcome.ran and "key" in outcome.message and not toy.console.prompts
    assert toy.ob.missing(toy.node("net")) == ["key"]


async def test_a_probe_that_needs_a_secret_says_so_instead_of_prompting(toy):
    answer(toy, who="ann", nick="", color="red")
    toy.flags["base"] = True
    status = await toy.ob.detect(toy.node("keyed"), fresh=True)
    assert status.state == "missing" and status.detail == "needs key"
    assert not toy.console.prompts  # a status check must never stop to ask for a password
    toy.secrets.set("key", "k" * 8, persist=False)
    assert (await toy.ob.detect(toy.node("keyed"), fresh=True)).done


async def test_only_actions_may_ask_for_a_secret(toy):
    probe_ctx = toy.ob.context(None)
    with pytest.raises(MissingInput, match="needs key"):
        await probe_ctx.secret("key")
    assert not toy.console.prompts
    toy.console.answers[:] = ["typed-secret-value"]
    assert await toy.ob.context(None, interactive=True).secret("key") == "typed-secret-value"
    assert toy.console.prompts[-1][1] is True  # asked hidden
    assert len(await toy.ob.context(None, interactive=True).secret("auto")) >= 32  # generated, not asked
