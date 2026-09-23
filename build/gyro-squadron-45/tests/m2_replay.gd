extends SceneTree
## Milestone 2 scripted replays (headless, deterministic):
##   godot --headless --script res://tests/m2_replay.gd
## Run A "weave and survive": ~20 simulated seconds of full-deflection sweeps
## through the REAL main scene — no errors, plane alive + in bounds every
## sampled frame, score increases, stage keeps producing waves (no soft-lock).
## Run B "eat everything": plane parked dead center until lives hit 0 ->
## GAME OVER shows; then the tap-retry path (restart()) resets the run.
## The steering is injected through the same TiltSource the device uses.

const MAIN_SCENE := preload("res://scenes/main.tscn")
const DT := 1.0 / 60.0
const WEAVE_SECONDS := 20.0
const EAT_CAP_SEC := 30.0       # bound for the eat-everything death

var passes := 0
var fails := 0


func _initialize() -> void:
	_run()


func _run() -> void:
	await process_frame

	var main: Node2D = MAIN_SCENE.instantiate()
	root.add_child(main)             # _ready runs here: stage + HUD + buttons live
	main.set_process(false)          # replays step the game by hand, _process never runs
	await process_frame
	await process_frame
	main.tilt.set_mode(TiltSource.MODE_SYNTHETIC)

	# ============================ run A: weave and survive =================
	var plane: PlayerPlane = main.plane
	var stage: Stage = main.stage
	var view := plane.get_viewport_rect().size
	var out_of_bounds := 0
	var samples := 0
	var score_start := stage.score
	var min_x := 1e12
	var max_x := -1e12
	var frames := int(WEAVE_SECONDS * 60.0)
	# Closed-loop edge-to-edge weave: hold full deflection until the plane
	# reaches a clamp edge, then reverse. Covers every column whatever the
	# viewport width (script mode has no stretch).
	var raw := 1.0
	var flip_zone := 4.0
	for i in frames:
		main.tilt.push_tilt(raw)
		main.sim_frame(DT)
		if not is_instance_valid(plane):
			break
		if raw > 0.0 and plane.position.x >= view.x - Feel.CLAMP_MARGIN - flip_zone:
			raw = -1.0
		elif raw < 0.0 and plane.position.x <= Feel.CLAMP_MARGIN + flip_zone:
			raw = 1.0
		min_x = minf(min_x, plane.position.x)
		max_x = maxf(max_x, plane.position.x)
		var margin := Feel.CLAMP_MARGIN - 0.001
		if plane.position.x < margin or plane.position.x > view.x - margin:
			out_of_bounds += 1
		samples += 1
	var weave_alive: bool = is_instance_valid(plane) and not main.is_game_over() \
		and stage.lives > 0
	check("weave_survives_twenty_seconds", weave_alive, "lives=%d over=%s" % [
		stage.lives, main.is_game_over()])
	check("weave_plane_in_bounds_every_frame", out_of_bounds == 0 and samples == frames,
		"oob=%d samples=%d" % [out_of_bounds, samples])
	check("weave_covers_both_extremes", min_x <= Feel.CLAMP_MARGIN + flip_zone \
		and max_x >= view.x - Feel.CLAMP_MARGIN - flip_zone,
		"x=[%f, %f] width=%f" % [min_x, max_x, view.x])
	check("weave_score_increases", stage.score > score_start and stage.kills > 0,
		"score %d -> %d kills=%d" % [score_start, stage.score, stage.kills])
	check("weave_no_softlock_waves_keep_coming", stage.time >= WEAVE_SECONDS \
		and stage.time <= WEAVE_SECONDS + 0.5 \
		and stage.state == Stage.ST_WAVES and stage.fighters_spawned > 0 \
		and stage.bombers_spawned > 0 and stage.player_bullets.spawned_total > 0,
		"time=%f state=%d fighters=%d bombers=%d shots=%d" % [stage.time, stage.state,
		stage.fighters_spawned, stage.bombers_spawned, stage.player_bullets.spawned_total])

	# ============================ run B: eat everything ====================
	main.restart()
	await process_frame
	await process_frame
	plane = main.plane
	stage = main.stage
	var eaten_frames := 0
	var cap := int(EAT_CAP_SEC * 60.0)
	while not main.is_game_over() and eaten_frames < cap:
		main.tilt.push_tilt(0.0)  # parked dead center, eats everything
		main.sim_frame(DT)
		eaten_frames += 1
	check("eat_everything_reaches_game_over", main.is_game_over() and stage.lives <= 0
		and eaten_frames < cap, "lives=%d frames=%d" % [stage.lives, eaten_frames])
	check("game_over_overlay_shows", main.game_over_shown and main.hud.overlay_visible()
		and main.hud.overlay_title.text == "GAME OVER",
		"flag=%s visible=%s title=%s" % [main.game_over_shown,
		main.hud.overlay_visible(), main.hud.overlay_title.text])

	# ============================ tap-retry path ===========================
	main.restart()  # the exact code path the tilt-agnostic tap triggers
	await process_frame
	await process_frame
	check("retry_resets_the_run", not main.is_game_over() and not main.hud.overlay_visible() \
		and main.stage.lives == Feel.PLAYER_LIVES and main.stage.state == Stage.ST_WAVES \
		and main.stage.time <= DT * 2.0 and main.stage.score == 0
		and main.stage.weapon.tier == Feel.WEAPON_TIER_MIN \
		and main.stage.weapon.kills_charged == 0,
		"over=%s lives=%d state=%d time=%f score=%d" % [main.is_game_over(),
		main.stage.lives, main.stage.state, main.stage.time, main.stage.score])
	var still_steps: bool = true
	for i in 60:
		main.tilt.push_tilt(0.5)
		main.sim_frame(DT)
	still_steps = still_steps and main.stage.time > 0.5 and not main.is_game_over()
	check("retried_run_keeps_simulating", still_steps, "time=%f" % main.stage.time)

	print("")
	print("REPLAY: weave %d frames (%.1f s), eat %d frames, x in [%.0f, %.0f]" % [
		frames, WEAVE_SECONDS, eaten_frames, min_x, max_x])
	print("RESULT: %d passed, %d failed" % [passes, fails])
	quit(0 if fails == 0 else 1)


func check(name: String, ok: bool, detail := "") -> void:
	if ok:
		passes += 1
		print("PASS: " + name)
	else:
		fails += 1
		print("FAIL: " + name + "  (" + detail + ")")


func nearf(a: float, b: float, eps: float) -> bool:
	return absf(a - b) < eps
