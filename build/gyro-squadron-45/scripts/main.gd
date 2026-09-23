extends Node2D
## GYRO SQUADRON '45 — Act 1 + Act 2 (milestone 3).
## Milestone 1 control rig is intact: tilt drives the plane, RECENTER
## re-captures neutral, the mode toggle routes a finger drag through the SAME
## TiltSource. Milestone 2 adds the Stage driver (waves -> boss -> tally),
## the weapon/super/bomb actions, the HUD and the GAME OVER retry. Milestone 3
## rolls the run on into ACT 2 via the non-blocking banner after the act-1
## tally.
## All simulation flows through sim_frame(delta) so tests/replays can step the
## whole game by hand (set_process(false) + manual calls).

const SHOW_DEBUG := true

var tilt := TiltSource.new()
var touch_mode := false
var stage: Stage = null
var game_over_shown := false  # replay/test assertion surface

var _debug_frames := 0
var _bomb_button: Button
var _super_button: Button

@onready var plane: PlayerPlane = $Plane
@onready var recenter_button: Button = $UI/RecenterButton
@onready var mode_button: Button = $UI/ModeButton
@onready var debug_label: Label = $UI/DebugLabel
@onready var hud: Hud = $Hud


func _ready() -> void:
	recenter_button.pressed.connect(_on_recenter_pressed)
	mode_button.pressed.connect(_on_mode_pressed)
	debug_label.visible = SHOW_DEBUG
	_make_action_buttons()
	_start_stage()
	capture_neutral.call_deferred()  # spec: neutral captured on level start


func _make_action_buttons() -> void:
	var view := get_viewport_rect().size
	_bomb_button = Button.new()
	_bomb_button.text = "BOMB"
	_bomb_button.position = Vector2(16.0, view.y - 92.0)
	_bomb_button.size = Vector2(150.0, 68.0)
	_bomb_button.pressed.connect(_on_bomb_pressed)
	$UI.add_child(_bomb_button)
	_super_button = Button.new()
	_super_button.text = "SUPER"
	_super_button.position = Vector2(view.x - 166.0, view.y - 92.0)
	_super_button.size = Vector2(150.0, 68.0)
	_super_button.pressed.connect(_on_super_pressed)
	$UI.add_child(_super_button)


func _start_stage() -> void:
	if stage != null:
		stage.queue_free()
	stage = Stage.new()
	stage.name = "Stage"
	add_child(stage)
	move_child(stage, 1)  # Sea, Stage, Plane -> plane renders on top
	stage.bind_plane(plane)
	stage.start()
	stage.game_over.connect(_on_stage_game_over)
	stage.stage_cleared.connect(_on_stage_cleared)
	hud.hide_overlay()
	hud.set_banner("", false)
	game_over_shown = false
	plane.velocity = Vector2.ZERO
	plane.position = Vector2(
		get_viewport_rect().size.x * 0.5,
		get_viewport_rect().size.y * Feel.PLANE_START_FRACTION)


func capture_neutral() -> void:
	tilt.capture_neutral()


func is_game_over() -> bool:
	return stage != null and stage.state == Stage.ST_OVER


## One frame of the whole game. The ONLY update path (real play and replays).
func sim_frame(delta: float) -> void:
	if stage == null:
		return
	var output := tilt.read_output()
	plane.tilt_input = output
	stage.advance(delta, output)
	hud.update_from(stage)
	hud.set_banner(stage.banner_text(), stage.state == Stage.ST_BANNER)
	_refresh_buttons()
	_debug_frames += 1
	if SHOW_DEBUG and _debug_frames % Feel.DEBUG_SAMPLE_EVERY_N_FRAMES == 0:
		debug_label.text = "raw %+.3f   cal %+.3f   out %+.3f\nmode %s   tier %d   charge %d" % [
			tilt.read_raw(), tilt.read_calibrated(), output,
			tilt.mode, plane.weapon.tier, plane.weapon.kills_charged,
		]


func _process(delta: float) -> void:
	sim_frame(delta)


func _unhandled_input(event: InputEvent) -> void:
	if touch_mode:
		if event is InputEventScreenTouch:
			if event.pressed:
				tilt.touch_begin(event.position.x)
			else:
				tilt.touch_end()
		elif event is InputEventScreenDrag:
			tilt.touch_move(event.position.x)
	if event is InputEventScreenTouch and event.pressed and hud.overlay_visible():
		restart()  # tilt-agnostic tap: retry / fly again


func _on_recenter_pressed() -> void:
	capture_neutral()


func _on_mode_pressed() -> void:
	touch_mode = not touch_mode
	tilt.set_mode(TiltSource.MODE_TOUCH if touch_mode else TiltSource.MODE_DEVICE)
	capture_neutral()
	_update_mode_button()


func _update_mode_button() -> void:
	mode_button.text = "INPUT: TOUCH DRAG" if touch_mode else "INPUT: TILT"


func _on_bomb_pressed() -> void:
	stage.use_bomb()


func _on_super_pressed() -> void:
	stage.try_release_super()


func _on_stage_game_over() -> void:
	game_over_shown = true
	hud.show_game_over()


func _on_stage_cleared(tally: Dictionary) -> void:
	if stage.has_next_act():
		return  # the act banner takes over; the run continues into act 2
	hud.show_tally(tally)


func _refresh_buttons() -> void:
	_bomb_button.text = "BOMB %d" % stage.bombs
	var charged := plane.weapon.super_ready()
	_super_button.text = "SUPER!" if charged else "SUPER"
	_super_button.disabled = not charged


## Full reset for RETRY (tap on the GAME OVER / tally overlay).
func restart() -> void:
	_start_stage()
