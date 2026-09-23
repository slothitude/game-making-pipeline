extends SceneTree
## Milestone 1 tilt battery — run headless:
##   godot --headless --script res://tests/tilt_tests.gd
## Done = 2x consecutive green (spec law "growing_battery").

const PLANE_SCENE := preload("res://scenes/plane.tscn")
const DT := 1.0 / 60.0
const EPS := 1e-6

var passes := 0
var fails := 0


func _initialize() -> void:
	_run()


func _run() -> void:
	await process_frame

	# --- pure shaping: dead zone, curve, sensitivity -------------------------
	var src := TiltSource.new()
	src.set_mode(TiltSource.MODE_SYNTHETIC)

	src.push_tilt(0.05)
	check("dead_zone_below_positive_returns_exact_zero", src.read_output() == 0.0, "out=%f" % src.read_output())

	src.push_tilt(-0.05)
	check("dead_zone_below_negative_returns_exact_zero", src.read_output() == 0.0, "out=%f" % src.read_output())

	src.push_tilt(0.5)
	check("curve_positive_half", near(src.read_output(), 0.25 * Feel.SENSITIVITY), "out=%f" % src.read_output())

	src.push_tilt(-0.5)
	check("curve_negative_half", near(src.read_output(), -0.25 * Feel.SENSITIVITY), "out=%f" % src.read_output())

	src.push_tilt(1.0)
	var sat_pos: bool = src.read_output() == Feel.OUTPUT_MAX
	src.push_tilt(-1.0)
	var sat_neg: bool = src.read_output() == -Feel.OUTPUT_MAX
	check("curve_saturates_at_output_max", sat_pos and sat_neg, "sat_pos=%s sat_neg=%s" % [sat_pos, sat_neg])

	# --- calibration ---------------------------------------------------------
	var cal := TiltSource.new()
	cal.set_mode(TiltSource.MODE_SYNTHETIC)
	cal.push_tilt(0.3)
	cal.capture_neutral()
	cal.push_tilt(0.3)
	check("calibration_zeroes_identical_input", cal.read_output() == 0.0, "out=%f" % cal.read_output())

	cal.push_tilt(0.36)
	var small: float = cal.read_output()
	check("calibration_just_past_dead_zone_is_small_positive", small > 0.0 and small < 0.02, "out=%f" % small)

	cal.push_tilt(-0.2)
	cal.capture_neutral()
	cal.push_tilt(-0.2)
	check("recenter_adopts_new_neutral", cal.read_output() == 0.0, "out=%f" % cal.read_output())

	# --- pure math: gyro blend + gravity normalization ------------------------
	var blended: float = TiltSource.blend_gravity_gyro(0.5, 1.0)
	check("gyro_blend_formula", near(blended, 0.5 + (1.0 / Feel.GYRO_NORM_SCALE) * Feel.TILT_BLEND_GYRO), "blend=%f" % blended)
	check("gyro_blend_zero_gyro_identity", near(TiltSource.blend_gravity_gyro(0.5, 0.0), 0.5), "blend=%f" % TiltSource.blend_gravity_gyro(0.5, 0.0))

	var gravity_tilt: float = TiltSource.normalize_gravity(Vector3(4.905, -8.494, 0.0))
	check("gravity_normalization_30deg", near(gravity_tilt, 0.5), "norm=%f" % gravity_tilt)

	# --- plane: clamp at both edges + synthetic feeder steering ---------------
	var plane: PlayerPlane = PLANE_SCENE.instantiate()
	root.add_child(plane)
	await process_frame
	await process_frame
	plane.set_physics_process(false)  # battery steps it deterministically
	plane.position = Vector2(root.get_visible_rect().size.x * 0.5, 634.0)
	print("  viewport=%s plane_start=%s" % [plane.get_viewport_rect(), plane.position])

	for i in 240:  # 4 simulated seconds hard left
		plane.step(-1.0, DT)
	var width := plane.get_viewport_rect().size.x
	var left_ok := plane.position.x >= Feel.CLAMP_MARGIN - 0.001 and plane.position.x <= Feel.CLAMP_MARGIN + 0.001
	check("clamp_extreme_left_edge", left_ok, "x=%f" % plane.position.x)

	for i in 240:  # then 4 simulated seconds hard right
		plane.step(1.0, DT)
	var right_edge := width - Feel.CLAMP_MARGIN
	var right_ok := plane.position.x >= right_edge - 0.001 and plane.position.x <= right_edge + 0.001
	check("clamp_extreme_right_edge", right_ok, "x=%f limit=%f" % [plane.position.x, right_edge])

	var feeder := TiltSource.new()
	feeder.set_mode(TiltSource.MODE_SYNTHETIC)
	plane.position.x = width * 0.5
	feeder.push_tilt(-1.0)
	for i in 10:
		plane.step(feeder.read_output(), DT)
	var went_left := plane.position.x < width * 0.5
	feeder.push_tilt(1.0)
	var before := plane.position.x
	for i in 10:
		plane.step(feeder.read_output(), DT)
	var went_right := plane.position.x > before
	check("synthetic_feeder_drives_plane_both_ways", went_left and went_right, "left=%s right=%s" % [went_left, went_right])

	# --- touch feeder ---------------------------------------------------------
	var touch := TiltSource.new()
	touch.set_mode(TiltSource.MODE_TOUCH)
	touch.touch_begin(270.0)
	touch.touch_move(270.0 + Feel.TOUCH_DRAG_RANGE_PX * 0.5)
	check("touch_delta_maps_half_deflection", near(touch.read_output(), pow(0.5, Feel.CURVE_EXPONENT) * Feel.SENSITIVITY), "out=%f" % touch.read_output())

	touch.touch_begin(270.0)
	touch.touch_move(270.0 - Feel.TOUCH_DRAG_RANGE_PX * 0.5)
	check("touch_delta_negative_sign", touch.read_output() < 0.0, "out=%f" % touch.read_output())

	touch.touch_end()
	check("touch_release_returns_to_zero", touch.read_output() == 0.0, "out=%f" % touch.read_output())

	plane.queue_free()
	print("")
	print("RESULT: %d passed, %d failed" % [passes, fails])
	quit(0 if fails == 0 else 1)


func check(name: String, ok: bool, detail := "") -> void:
	if ok:
		passes += 1
		print("PASS: " + name)
	else:
		fails += 1
		print("FAIL: " + name + "  (" + detail + ")")


func near(a: float, b: float) -> bool:
	return absf(a - b) < EPS
