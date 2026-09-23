extends SceneTree
## Scripted synthetic tilt replay — ~6 simulated seconds of sine sway with
## forced extremes, sampled every frame. Gate: no errors, plane alive, in
## bounds on every sampled frame.
##   godot --headless --script res://tests/tilt_replay.gd

const PLANE_SCENE := preload("res://scenes/plane.tscn")
const DT := 1.0 / 60.0
const DURATION := 6.0
const SWAY_PERIOD := 3.0  # seconds per full sway; extremes forced in addition

var passes := 0
var fails := 0


func _initialize() -> void:
	_run()


func _run() -> void:
	await process_frame

	var tilt := TiltSource.new()
	tilt.set_mode(TiltSource.MODE_SYNTHETIC)

	var plane: PlayerPlane = PLANE_SCENE.instantiate()
	root.add_child(plane)
	await process_frame
	await process_frame
	plane.set_physics_process(false)
	var view := plane.get_viewport_rect().size
	plane.position = Vector2(view.x * 0.5, view.y * Feel.PLANE_START_FRACTION)

	var frames := int(DURATION / DT)
	var raw_min := 10.0
	var raw_max := -10.0
	var out_of_bounds := 0
	var died := false

	for i in frames:
		var t := float(i) * DT
		var raw := sin(TAU * t / SWAY_PERIOD)
		if i % 90 == 45:  # force hard extremes into the sway
			raw = 1.0 if (i / 90) % 2 == 0 else -1.0
		tilt.push_tilt(raw)
		plane.step(tilt.read_output(), DT)

		raw_min = minf(raw_min, raw)
		raw_max = maxf(raw_max, raw)
		if not is_instance_valid(plane):
			died = true
			break
		var margin := Feel.CLAMP_MARGIN - 0.001
		if plane.position.x < margin or plane.position.x > view.x - margin:
			out_of_bounds += 1

	check("replay_plane_alive", not died, "died at frame sample")
	check("replay_hit_both_extremes", raw_max >= 1.0 and raw_min <= -1.0, "min=%f max=%f" % [raw_min, raw_max])
	check("replay_stayed_in_bounds_every_frame", out_of_bounds == 0, "out_of_bounds_frames=%d" % out_of_bounds)
	check("replay_full_duration_ran", frames == int(DURATION * 60.0), "frames=%d" % frames)

	plane.queue_free()
	print("")
	print("REPLAY: %d frames sampled (%.1f s), raw in [%.2f, %.2f], final x=%.1f" % [
		frames, float(frames) * DT, raw_min, raw_max, plane.position.x,
	])
	print("RESULT: %d passed, %d failed" % [passes, fails])
	quit(0 if fails == 0 else 1)


func check(name: String, ok: bool, detail := "") -> void:
	if ok:
		passes += 1
		print("PASS: " + name)
	else:
		fails += 1
		print("FAIL: " + name + "  (" + detail + ")")
