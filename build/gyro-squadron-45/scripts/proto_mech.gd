class_name ProtoMech
extends Node2D
## Act-2 boss (milestone 3): proto_mech, hp Feel.PROTO_HP, phase 2 at
## Feel.PROTO_PHASE2_HP. Enters from the top, drifts on a sine, and cycles
## between two patterns:
##   missile arc — a volley of lobbed projectiles (upward launch, gravity
##                 pulls them down onto the player's column)
##   laser sweep — telegraphed horizontal beam: warning line, then burn
## Phase 1 alternates the patterns (never both at once); phase 2 runs both
## simultaneously. Death -> defeated signal -> Stage plays the clear beat.
## Hand-stepped state machine, no physics: deterministic for tests/replays.

signal defeated
signal phase_changed(phase: int)

const PH_ENTER := 0
const PH_ONE := 1
const PH_TWO := 2
const PH_DEAD := 3

const LS_IDLE := 0
const LS_TELEGRAPH := 1
const LS_BEAM := 2

var hp := Feel.PROTO_HP
var max_hp := Feel.PROTO_HP
var radius := Feel.PROTO_RADIUS
var score_value := Feel.PROTO_SCORE
var phase := PH_ENTER
var alive := true
var shots_fired := 0
var missile_volleys := 0
var telegraphs_started := 0
var laser_fired := 0
var laser_y := 0.0             # world row the horizontal beam burns

var pool: BulletPool = null    # enemy bullet pool (injected)
var target: Node2D = null      # player plane for the laser aim (may be null)

var _t := 0.0
var _anchor_x := 0.0
var _missiles_enabled := false
var _laser_enabled := false
var _missile_timer := 0.0
var _missile_queue := 0
var _missile_gap := 0.0
var _missile_idx := 0
var _laser_state := LS_IDLE
var _laser_t := 0.0
var _window := 0.0             # phase 1 pattern-change countdown


func _ready() -> void:
	_anchor_x = position.x


func phase2() -> bool:
	return phase == PH_TWO


## Seconds of laser idle before the next telegraph at the current phase.
func laser_period() -> float:
	return Feel.PROTO_LASER_PERIOD_P2 if phase == PH_TWO else Feel.PROTO_LASER_PERIOD


func laser_telegraphing() -> bool:
	return _laser_state == LS_TELEGRAPH


## 0..1 progress through the current telegraph (test seam + drawing).
func telegraph_progress() -> float:
	if _laser_state != LS_TELEGRAPH:
		return 0.0
	return clampf(_laser_t / Feel.PROTO_LASER_TELEGRAPH_SEC, 0.0, 1.0)


func is_beam_active() -> bool:
	return alive and _laser_state == LS_BEAM


func beam_hits(pos: Vector2) -> bool:
	if not is_beam_active():
		return false
	var half := Feel.PROTO_LASER_WIDTH_PX * 0.5
	return absf(pos.y - laser_y) <= half + Feel.PLAYER_RADIUS


func step(delta: float) -> void:
	if not alive:
		return
	_t += delta
	match phase:
		PH_ENTER:
			position.y += Feel.PROTO_ENTER_SPEED * delta
			if position.y >= Feel.PROTO_ENTER_Y:
				position.y = Feel.PROTO_ENTER_Y
				phase = PH_ONE
				_window = Feel.PROTO_PATTERN_REST_SEC
				_missiles_enabled = true
				_missile_timer = Feel.PROTO_MISSILE_FIRST_DELAY
				_laser_enabled = false
				_laser_state = LS_IDLE
				_laser_t = 0.0
				phase_changed.emit(phase)
		PH_ONE, PH_TWO:
			position.x = _anchor_x + sin(_t * Feel.PROTO_DRIFT_SPEED_RAD) \
					* Feel.PROTO_DRIFT_AMPLITUDE_PX
			if phase == PH_ONE:
				_step_window(delta)
			_step_missiles(delta)
			_step_laser(delta)


## Phase 1 alternation: hold a pattern until its machines go quiet, rest, then
## swap. Phase 2 runs both at once, so this never fires there.
func _step_window(delta: float) -> void:
	if _missile_queue > 0 or _laser_state != LS_IDLE:
		return
	_window -= delta
	if _window <= 0.0:
		_window = Feel.PROTO_PATTERN_REST_SEC
		_missiles_enabled = not _missiles_enabled
		_laser_enabled = not _laser_enabled
		if _missiles_enabled:
			_missile_timer = Feel.PROTO_MISSILE_FIRST_DELAY
		if _laser_enabled:
			_laser_t = 0.0


func _step_missiles(delta: float) -> void:
	if not _missiles_enabled:
		_missile_queue = 0
		return
	if _missile_queue > 0:
		_missile_gap -= delta
		if _missile_gap <= 0.0:
			_missile_gap = Feel.PROTO_MISSILE_GAP_SEC
			_missile_queue -= 1
			_lob_missile()
	else:
		_missile_timer -= delta
		if _missile_timer <= 0.0:
			_missile_timer = Feel.PROTO_MISSILE_PERIOD
			_missile_queue = Feel.PROTO_MISSILE_COUNT
			_missile_gap = 0.0   # first missile of the volley leaves at once
			_missile_idx = 0
			missile_volleys += 1


func _lob_missile() -> void:
	if pool == null:
		return
	var fan := float(Feel.PROTO_MISSILE_COUNT - 1)
	var vx := lerpf(-1.0, 1.0, float(_missile_idx) / fan) * Feel.PROTO_MISSILE_VX
	_missile_idx += 1
	var vel := Vector2(vx, -Feel.PROTO_MISSILE_VY_UP)   # lobbed: up, then gravity
	if pool.spawn(global_position + Feel.PROTO_MISSILE_MUZZLE, vel,
			Feel.ENEMY_BULLET_DAMAGE, Feel.PROTO_MISSILE_RADIUS, false,
			Feel.PROTO_MISSILE_GRAVITY) != null:
		shots_fired += 1


func _step_laser(delta: float) -> void:
	if not _laser_enabled:
		_laser_state = LS_IDLE
		_laser_t = 0.0
		return
	_laser_t += delta
	match _laser_state:
		LS_IDLE:
			if _laser_t >= laser_period():
				_laser_state = LS_TELEGRAPH
				_laser_t = 0.0
				telegraphs_started += 1
				laser_y = _aim_laser_y()
		LS_TELEGRAPH:
			if _laser_t >= Feel.PROTO_LASER_TELEGRAPH_SEC:
				_laser_state = LS_BEAM
				_laser_t = 0.0
				laser_fired += 1
		LS_BEAM:
			if _laser_t >= Feel.PROTO_LASER_BEAM_SEC:
				_laser_state = LS_IDLE
				_laser_t = 0.0


func _aim_laser_y() -> float:
	if target != null and is_instance_valid(target):
		return target.global_position.y
	return global_position.y + Feel.PROTO_LASER_FALLBACK_Y


## Returns true on the killing blow.
func take_damage(amount: int) -> bool:
	if not alive:
		return false
	hp = maxi(0, hp - amount)
	if phase == PH_ONE and hp <= Feel.PROTO_PHASE2_HP:
		phase = PH_TWO
		_missiles_enabled = true
		_laser_enabled = true
		if _missile_queue == 0:
			_missile_timer = Feel.PROTO_MISSILE_PERIOD
		if _laser_state == LS_IDLE:
			_laser_t = 0.0
		phase_changed.emit(phase)
	if hp <= 0:
		alive = false
		phase = PH_DEAD
		_missile_queue = 0
		_laser_state = LS_IDLE
		defeated.emit()
		return true
	return false


func _draw() -> void:
	# original_assets (procedural law): squat black-budget testbed mech.
	var s := Feel.PROTO_RADIUS
	if is_beam_active():
		var view := get_viewport_rect().size
		var half := Feel.PROTO_LASER_WIDTH_PX * 0.5
		var ly := laser_y - position.y
		draw_rect(Rect2(-position.x, ly - half, view.x, half * 2.0), Feel.PROTO_BEAM_COLOR)
	elif laser_telegraphing():
		var view := get_viewport_rect().size
		var ly := laser_y - position.y
		var w := view.x * telegraph_progress()
		draw_rect(Rect2(-position.x, ly - 2.0, w, 4.0), Feel.PROTO_TELEGRAPH_COLOR)
	draw_rect(Rect2(-s * 0.62, -s * 0.55, s * 1.24, s * 1.05), Feel.PROTO_ARMOR_COLOR)
	draw_rect(Rect2(-s * 0.5, -s * 0.42, s * 1.0, s * 0.5), Feel.PROTO_PLATE_COLOR)
	draw_rect(Rect2(-s * 1.05, -s * 0.5, s * 0.42, s * 0.85), Feel.PROTO_ARMOR_COLOR)   # shoulders
	draw_rect(Rect2(s * 0.63, -s * 0.5, s * 0.42, s * 0.85), Feel.PROTO_ARMOR_COLOR)
	draw_rect(Rect2(-s * 0.2, -s * 0.85, s * 0.4, s * 0.35), Feel.PROTO_PLATE_COLOR)    # head
	draw_circle(Vector2(0.0, -s * 0.68), s * 0.1, Feel.PROTO_GLOW_COLOR)
	draw_circle(Vector2(-s * 0.84, -s * 0.07), s * 0.13, Feel.PROTO_GLOW_COLOR)
	draw_circle(Vector2(s * 0.84, -s * 0.07), s * 0.13, Feel.PROTO_GLOW_COLOR)
