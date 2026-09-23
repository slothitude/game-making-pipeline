class_name BossFortress
extends Node2D
## Act-1 boss: bomber_fortress, hp Feel.BOSS1_HP. Enters from the top after
## the stage timer, drifts on a sine, and cycles patterns by phase:
##   phase 1: 5-way spread sweeps (wobbling aim, volley to volley)
##   phase 2 (hp <= Feel.BOSS1_PHASE2_HP): faster sweeps + aimed bursts
## Death -> defeated signal -> Stage plays the big explosion + clear tally.
## Hand-stepped state machine, no physics: deterministic for tests/replays.

signal defeated
signal phase_changed(phase: int)

const PH_ENTER := 0
const PH_ONE := 1
const PH_TWO := 2
const PH_DEAD := 3

var hp := Feel.BOSS1_HP
var max_hp := Feel.BOSS1_HP
var radius := Feel.BOSS1_RADIUS
var score_value := Feel.BOSS1_SCORE
var phase := PH_ENTER
var alive := true
var shots_fired := 0
var bursts_fired := 0
var sweeps_fired := 0

var pool: BulletPool = null   # enemy bullet pool (injected)
var target: Node2D = null     # player plane for aimed bursts (may be null)

var _t := 0.0
var _anchor_x := 0.0
var _sweep_timer := Feel.BOSS1_SWEEP_INTERVAL
var _wobble := 0.0
var _wobble_dir := 1.0
var _burst_timer := Feel.BOSS1_BURST_PERIOD
var _burst_queue := 0
var _burst_gap := 0.0


func _ready() -> void:
	_anchor_x = position.x
	var path := "res://assets/generated/boss_fortress.png"
	if ResourceLoader.exists(path):
		var spr := Sprite2D.new()
		spr.texture = load(path)
		spr.scale = Vector2.ONE * Feel.BOSS1_SPRITE_SCALE
		add_child(spr)


func phase2() -> bool:
	return phase == PH_TWO


## Beam seam (shared with ProtoMech): the Act-1 fortress has no laser.
func is_beam_active() -> bool:
	return false


func beam_hits(_pos: Vector2) -> bool:
	return false


## Seconds between sweeps at the current phase (the phase-2 speedup lives here).
func sweep_interval() -> float:
	return Feel.BOSS1_SWEEP_INTERVAL_P2 if phase == PH_TWO else Feel.BOSS1_SWEEP_INTERVAL


func step(delta: float) -> void:
	if not alive:
		return
	_t += delta
	match phase:
		PH_ENTER:
			position.y += Feel.BOSS1_ENTER_SPEED * delta
			if position.y >= Feel.BOSS1_ENTER_Y:
				position.y = Feel.BOSS1_ENTER_Y
				phase = PH_ONE
				_sweep_timer = Feel.BOSS1_SWEEP_INTERVAL
				phase_changed.emit(phase)
		PH_ONE, PH_TWO:
			position.x = _anchor_x + sin(_t * Feel.BOSS1_DRIFT_SPEED_RAD) * Feel.BOSS1_DRIFT_AMPLITUDE_PX
			_step_sweep(delta)
			if phase == PH_TWO:
				_step_bursts(delta)


func _step_sweep(delta: float) -> void:
	_sweep_timer -= delta
	if _sweep_timer > 0.0:
		return
	_sweep_timer += sweep_interval()
	# 5-way spread across the arc; angle 0 = straight down (y-down coords),
	# whole fan wobbles volley to volley so the sweeps rake the screen.
	var base := _wobble
	var arms := Feel.BOSS1_SWEEP_COUNT
	for i in arms:
		var a := base + lerpf(-Feel.BOSS1_SWEEP_ARC_RAD, Feel.BOSS1_SWEEP_ARC_RAD, float(i) / float(arms - 1)) * 0.5
		_fire(Vector2(sin(a), cos(a)), Feel.BOSS1_SWEEP_BULLET_SPEED)
	sweeps_fired += 1
	_wobble += _wobble_dir * Feel.BOSS1_SWEEP_WOBBLE_STEP_RAD
	if absf(_wobble) >= Feel.BOSS1_SWEEP_WOBBLE_RAD:
		_wobble = clampf(_wobble, -Feel.BOSS1_SWEEP_WOBBLE_RAD, Feel.BOSS1_SWEEP_WOBBLE_RAD)
		_wobble_dir = -_wobble_dir


func _step_bursts(delta: float) -> void:
	if _burst_queue > 0:
		_burst_gap -= delta
		if _burst_gap <= 0.0:
			_burst_gap = Feel.BOSS1_BURST_GAP_SEC
			_burst_queue -= 1
			var dir := Vector2.DOWN
			if target != null and is_instance_valid(target):
				dir = target.global_position - global_position
			_fire(dir, Feel.BOSS1_AIM_BULLET_SPEED)
			bursts_fired += 1
	else:
		_burst_timer -= delta
		if _burst_timer <= 0.0:
			_burst_timer = Feel.BOSS1_BURST_PERIOD
			_burst_queue = Feel.BOSS1_BURST_COUNT
			_burst_gap = 0.0


func _fire(dir: Vector2, speed: float) -> void:
	if pool == null:
		return
	if pool.spawn(global_position, dir.normalized() * speed,
			Feel.ENEMY_BULLET_DAMAGE, Feel.ENEMY_BULLET_RADIUS, false) != null:
		shots_fired += 1


## Returns true on the killing blow.
func take_damage(amount: int) -> bool:
	if not alive:
		return false
	hp = maxi(0, hp - amount)
	if phase == PH_ONE and hp <= Feel.BOSS1_PHASE2_HP:
		phase = PH_TWO
		phase_changed.emit(phase)
	if hp <= 0:
		alive = false
		phase = PH_DEAD
		_burst_queue = 0
		defeated.emit()
		return true
	return false


## Test seam: shots the boss will fire per sweep fan (5-way spread).
static func sweep_angles() -> int:
	return Feel.BOSS1_SWEEP_COUNT
