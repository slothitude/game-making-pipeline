class_name Enemy
extends Node2D
## Act-1/2 enemy base: hp, hit circle, score value, and a bullet pool to fire
## into. Patterns are tiny explicit state machines stepped by hand (no
## physics) so tests/replays are deterministic.
## Types (spec act 1): FighterStraight (hp 1, dives from the top, fires a
## single aimed shot on a timer) and BomberSlow (hp 4, slow sine drift across,
## drops a 3-shot spread downward).
## Types (spec act 2, milestone 3): UnmarkedJet (fast diver, 2-shot
## energy-bolt burst) and EnergyTurret (stationary on a screen edge, sweeping
## laser telegraph then a horizontal beam).

signal died(enemy: Enemy)

var kind := "fighter"
var hp := Feel.FIGHTER_HP
var radius := Feel.FIGHTER_RADIUS
var score_value := Feel.FIGHTER_SCORE
var alive := true
var shots_fired := 0

var pool: BulletPool = null   # enemy bullet pool (injected by Stage)
var target: Node2D = null     # player plane, for aimed shots (may be null)

const ST_DIVE := 0
const ST_FIRE := 1


func _init(enemy_kind := "fighter") -> void:
	kind = enemy_kind
	match kind:
		"bomber":
			hp = Feel.BOMBER_HP
			radius = Feel.BOMBER_RADIUS
			score_value = Feel.BOMBER_SCORE
			_build_sprite("res://assets/generated/enemy_bomber.png", Feel.BOMBER_SPRITE_SCALE)
		"jet":
			hp = Feel.JET_HP
			radius = Feel.JET_RADIUS
			score_value = Feel.JET_SCORE
		"turret":
			hp = Feel.TURRET_HP
			radius = Feel.TURRET_RADIUS
			score_value = Feel.TURRET_SCORE
		_:
			hp = Feel.FIGHTER_HP
			radius = Feel.FIGHTER_RADIUS
			score_value = Feel.FIGHTER_SCORE
			_build_sprite("res://assets/generated/enemy_fighter.png", Feel.FIGHTER_SPRITE_SCALE)


func _build_sprite(path: String, sprite_scale: float) -> void:
	if not ResourceLoader.exists(path):
		return
	var spr := Sprite2D.new()
	spr.texture = load(path)
	spr.scale = Vector2.ONE * sprite_scale
	spr.rotation = PI  # art faces up; these fly down/across
	add_child(spr)


## One frame of behaviour. Virtual.
func step(_delta: float) -> void:
	pass


## Returns true when this hit killed the enemy.
func take_damage(amount: int) -> bool:
	if not alive:
		return false
	hp -= amount
	if hp <= 0:
		alive = false
		died.emit(self)
		return true
	return false


## Off-screen cleanup check, called by the Stage after stepping.
func escaped() -> bool:
	var view := get_viewport_rect().size
	var m := Feel.BULLET_OFFSCREEN_MARGIN_PX + radius
	return position.y > view.y + m or position.y < -m * 3.0 \
			or position.x < -m or position.x > view.x + m


func _fire(dir: Vector2, speed: float) -> void:
	if pool == null:
		return
	if pool.spawn(global_position, dir.normalized() * speed,
			Feel.ENEMY_BULLET_DAMAGE, Feel.ENEMY_BULLET_RADIUS, false) != null:
		shots_fired += 1


## Beam seam (act 2 lasers): the Stage asks every enemy each frame. Default: none.
func is_beam_active() -> bool:
	return false


func beam_hits(_pos: Vector2) -> bool:
	return false


## ------------------------------------------------------------------ types --

class FighterStraight:
	extends Enemy
	## Dives from the top; on a timer snaps into a brief FIRE beat that looses
	## one aimed shot at the player, then back to diving.

	var state := ST_DIVE
	var state_time := 0.0
	var fire_timer := Feel.FIGHTER_FIRST_FIRE_DELAY

	func _init() -> void:
		super("fighter")

	func step(delta: float) -> void:
		state_time += delta
		position.y += Feel.FIGHTER_SPEED * delta
		match state:
			ST_DIVE:
				fire_timer -= delta
				if fire_timer <= 0.0:
					state = ST_FIRE
					state_time = 0.0
					_fire_aimed()
			ST_FIRE:
				if state_time >= Feel.FIGHTER_FIRE_STATE_SEC:
					state = ST_DIVE
					state_time = 0.0
					fire_timer = Feel.FIGHTER_FIRE_INTERVAL

	func _fire_aimed() -> void:
		var dir := Vector2.DOWN
		if target != null and is_instance_valid(target):
			dir = target.global_position - global_position
		_fire(dir, Feel.ENEMY_BULLET_SPEED)


class BomberSlow:
	extends Enemy
	## Slow sine drift across the screen; drops a 3-shot spread downward on a
	## timer. y(t) integrates the sine derivative so the drift is smooth and
	## deterministic.

	var dir_x := 1.0
	var drift_t := 0.0
	var fire_timer := Feel.BOMBER_FIRST_FIRE_DELAY
	var min_y := 1e12
	var max_y := -1e12

	func _init() -> void:
		super("bomber")

	func step(delta: float) -> void:
		drift_t += delta
		position.x += dir_x * Feel.BOMBER_SPEED * delta
		position.y += cos(drift_t * Feel.BOMBER_SINE_RAD_PER_SEC) \
				* Feel.BOMBER_SINE_AMPLITUDE_PX * Feel.BOMBER_SINE_RAD_PER_SEC * delta
		min_y = minf(min_y, position.y)
		max_y = maxf(max_y, position.y)
		fire_timer -= delta
		if fire_timer <= 0.0:
			fire_timer = Feel.BOMBER_FIRE_INTERVAL
			_fire_spread()

	func _fire_spread() -> void:
		var arms := Feel.BOMBER_SPREAD_COUNT
		for i in arms:
			var a := lerpf(-Feel.BOMBER_SPREAD_RAD, Feel.BOMBER_SPREAD_RAD, float(i) / float(arms - 1))
			_fire(Vector2(sin(a), cos(a)), Feel.BOMBER_BULLET_SPEED)  # spread points down


class UnmarkedJet:
	extends Enemy
	## Act-2 unmarked jet: fast straight dive from the top; on a timer snaps
	## into a BURST beat that looses Feel.JET_BURST_COUNT energy bolts (a quick
	## aimed pair, Feel.JET_BURST_GAP_SEC apart), then keeps diving.
	## (ST_DIVE is inherited from Enemy; the FIRE beat becomes the burst.)
	const ST_BURST := 2

	var state := ST_DIVE
	var state_time := 0.0
	var fire_timer := Feel.JET_FIRST_FIRE_DELAY
	var burst_queue := 0
	var burst_gap := 0.0
	var burst_times: Array[float] = []   # bolt launch stamps (test seam)

	func _init() -> void:
		super("jet")

	func step(delta: float) -> void:
		state_time += delta
		position.y += Feel.JET_SPEED * delta
		match state:
			ST_DIVE:
				fire_timer -= delta
				if fire_timer <= 0.0:
					state = ST_BURST
					burst_queue = Feel.JET_BURST_COUNT
					burst_gap = 0.0
			ST_BURST:
				burst_gap -= delta
				if burst_gap <= 0.0:
					burst_gap = Feel.JET_BURST_GAP_SEC
					burst_queue -= 1
					_fire_bolt()
					if burst_queue <= 0:
						state = ST_DIVE
						state_time = 0.0
						fire_timer = Feel.JET_FIRE_INTERVAL

	func _fire_bolt() -> void:
		var dir := Vector2.DOWN
		if target != null and is_instance_valid(target):
			dir = target.global_position - global_position
		_fire(dir, Feel.JET_BOLT_SPEED)
		burst_times.append(state_time)

	func _draw() -> void:
		# original_assets (procedural law): unmarked delta jet, nose down.
		var s := Feel.JET_RADIUS
		var pts := PackedVector2Array([
			Vector2(0.0, s * 1.2),
			Vector2(s * 0.8, -s * 0.7),
			Vector2(s * 0.3, -s * 0.4),
			Vector2(0.0, -s * 0.9),
			Vector2(-s * 0.3, -s * 0.4),
			Vector2(-s * 0.8, -s * 0.7),
			Vector2(0.0, s * 1.2),
		])
		draw_colored_polygon(pts, Feel.JET_BODY_COLOR)
		draw_polyline(pts, Feel.HUD_OUTLINE_COLOR, 2.5)
		draw_circle(Vector2(0.0, s * 0.25), s * 0.16, Feel.JET_TRIM_COLOR)


class EnergyTurret:
	extends Enemy
	## Act-2 energy turret: pinned to a screen edge (edge_dir = +1 fires
	## rightward). CHARGE -> TELEGRAPH (aim line sweeps onto the horizontal)
	## -> BEAM (horizontal row-burner) -> CHARGE ... After Feel.TURRET_STAY_SEC
	## it drops off the screen so the Stage can cull it.
	const ST_CHARGE := 0
	const ST_TELEGRAPH := 1
	const ST_BEAM := 2

	var state := ST_CHARGE
	var state_time := 0.0
	var charge_timer := Feel.TURRET_FIRST_DELAY
	var stay_left := Feel.TURRET_STAY_SEC
	var leaving := false
	var edge_dir := 1.0
	var beam_y := 0.0
	var beam_shots := 0

	func _init() -> void:
		super("turret")

	func step(delta: float) -> void:
		state_time += delta
		if leaving:
			position.y += Feel.JET_EXIT_SPEED * delta
			return
		stay_left -= delta
		if stay_left <= 0.0:
			leaving = true
			return
		match state:
			ST_CHARGE:
				charge_timer -= delta
				if charge_timer <= 0.0:
					state = ST_TELEGRAPH
					state_time = 0.0
					beam_y = global_position.y
			ST_TELEGRAPH:
				if state_time >= Feel.TURRET_TELEGRAPH_SEC:
					state = ST_BEAM
					state_time = 0.0
					beam_shots += 1
			ST_BEAM:
				if state_time >= Feel.TURRET_BEAM_SEC:
					state = ST_CHARGE
					state_time = 0.0
					charge_timer = Feel.TURRET_RECHARGE_SEC

	func is_beam_active() -> bool:
		return state == ST_BEAM

	func beam_hits(pos: Vector2) -> bool:
		if not is_beam_active():
			return false
		var half := Feel.TURRET_BEAM_WIDTH_PX * 0.5
		return absf(pos.y - beam_y) <= half + Feel.PLAYER_RADIUS

	## 0..1 progress through the sweeping telegraph (test seam + drawing).
	func telegraph_progress() -> float:
		if state != ST_TELEGRAPH:
			return 0.0
		return clampf(state_time / Feel.TURRET_TELEGRAPH_SEC, 0.0, 1.0)

	## Current aim angle off the horizontal (rad); sweeps to 0 during the
	## telegraph and holds 0 while the beam burns.
	func aim_angle() -> float:
		if state == ST_BEAM:
			return 0.0
		if state == ST_TELEGRAPH:
			return (1.0 - telegraph_progress()) * Feel.TURRET_TELEGRAPH_SWEEP_RAD * -edge_dir
		return -edge_dir * Feel.TURRET_TELEGRAPH_SWEEP_RAD

	func _draw() -> void:
		# original_assets (procedural law): edge pod + sweeping emitter.
		draw_circle(Vector2.ZERO, Feel.TURRET_RADIUS, Feel.HUD_OUTLINE_COLOR)
		draw_circle(Vector2.ZERO, Feel.TURRET_RADIUS - 5.0, Feel.TURRET_BASE_COLOR)
		var aim := Vector2(edge_dir * cos(aim_angle()), sin(aim_angle()))
		if state == ST_TELEGRAPH:
			draw_line(Vector2.ZERO, aim * get_viewport_rect().size.x,
					Feel.TURRET_TELEGRAPH_COLOR, 3.0)
		if state == ST_BEAM:
			var view := get_viewport_rect().size
			var half := Feel.TURRET_BEAM_WIDTH_PX * 0.5
			var w := (view.x - position.x) if edge_dir > 0.0 else position.x
			var x0 := 0.0 if edge_dir > 0.0 else -position.x
			draw_rect(Rect2(x0, -half, w, half * 2.0), Feel.TURRET_BEAM_COLOR)
		draw_line(Vector2.ZERO, aim * Feel.TURRET_RADIUS, Feel.TURRET_CORE_COLOR, 5.0)
		draw_circle(Vector2.ZERO, Feel.TURRET_RADIUS * 0.3, Feel.TURRET_CORE_COLOR)
