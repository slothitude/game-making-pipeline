class_name TiltSource
extends RefCounted
## THE injectable input abstraction (spec law "injectable_input").
## ALL steering flows through here: device sensors, a synthetic feeder for
## CI/tests, or a touch-drag fallback. No scene or phone required.
##
## Pipeline (in order, all pure):
##   raw reading -> calibration (subtract neutral) -> dead zone
##   -> quadratic curve -> sensitivity -> saturate at Feel.OUTPUT_MAX

const MODE_DEVICE := "device"
const MODE_SYNTHETIC := "synthetic"
const MODE_TOUCH := "touch"

var mode: String = MODE_DEVICE
var neutral := 0.0

var _synthetic_tilt := 0.0
var _touch_active := false
var _touch_anchor := 0.0
var _touch_x := 0.0


# ------------------------------------------------------------- pure helpers --

## Absolute device tilt as truth: gravity.x normalized to -1..1.
## NEVER integrates raw gyro rotation.
static func normalize_gravity(gravity: Vector3) -> float:
	return clampf(gravity.x / Feel.GRAVITY_NORM, -1.0, 1.0)


## Blend a touch of gyroscope x (rad/s) into the gravity reading for
## responsiveness. Gravity stays the source of truth.
static func blend_gravity_gyro(gravity_norm: float, gyro_x: float) -> float:
	var blended := gravity_norm + (gyro_x / Feel.GYRO_NORM_SCALE) * Feel.TILT_BLEND_GYRO
	return clampf(blended, -1.0, 1.0)


## Calibration + dead zone + quadratic curve + sensitivity. Pure.
static func shape(calibrated: float) -> float:
	if absf(calibrated) < Feel.DEAD_ZONE:
		return 0.0
	var curved := signf(calibrated) * pow(absf(calibrated), Feel.CURVE_EXPONENT)
	return clampf(curved * Feel.SENSITIVITY, -Feel.OUTPUT_MAX, Feel.OUTPUT_MAX)


# ------------------------------------------------------------------ reading --

## Current uncalibrated tilt reading, -1..1, for the active mode.
func read_raw() -> float:
	match mode:
		MODE_DEVICE:
			return blend_gravity_gyro(normalize_gravity(Input.get_gravity()), Input.get_gyroscope().x)
		MODE_SYNTHETIC:
			return clampf(_synthetic_tilt, -1.0, 1.0)
		MODE_TOUCH:
			if not _touch_active:
				return 0.0
			var delta := (_touch_x - _touch_anchor) / Feel.TOUCH_DRAG_RANGE_PX
			return clampf(delta, -1.0, 1.0)
	return 0.0


## Raw minus calibration neutral.
func read_calibrated() -> float:
	return read_raw() - neutral


## Final steering value, -1..1, ready to multiply by Feel.PLANE_MAX_SPEED.
func read_output() -> float:
	return shape(read_calibrated())


# -------------------------------------------------------------- calibration --

## Capture the current reading as neutral (level start + RECENTER tap).
func capture_neutral() -> void:
	neutral = read_raw()


# ----------------------------------------------------------------- feeders --

## Synthetic feeder — what CI/tests use. Never needs a phone.
func push_tilt(x: float) -> void:
	_synthetic_tilt = x


## Touch feeder: anchor the drag, then move its x, then end it.
func touch_begin(x: float) -> void:
	_touch_active = true
	_touch_anchor = x
	_touch_x = x


func touch_move(x: float) -> void:
	_touch_x = x


func touch_end() -> void:
	_touch_active = false
	_touch_anchor = 0.0
	_touch_x = 0.0


func set_mode(new_mode: String) -> void:
	mode = new_mode
	neutral = 0.0
	_synthetic_tilt = 0.0
	touch_end()
