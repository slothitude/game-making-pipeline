class_name MiniBossRobot
extends CharacterBody2D
## bosses_v1.miniboss_robot — slice C. hp 30 (Feel.BOSS_ROBOT_HP), phase 2 at 15.
## Holds its ground at the arena's right edge and alternates two attacks
## (spec pattern): a STOMP that sends a shockwave skimming the floor (jump it),
## and a LASER that sweeps down from on high and slams the floor line (jump it
## late, or be somewhere else). Phase 2 shortens the cycle and stomps both ways.
## Contact with the hull kills (one-hit law). Buried heroes are untouchable.
## Not an EnemyAgent: ride / bite / scare / burrow-drag pass it by.

signal died(points: int)
signal phase2_started
signal attacked(kind: String)

enum State { IDLE, WINDUP, RECOVER }

var hp: int = Feel.BOSS_ROBOT_HP
var phase := 1
var points: int = Feel.BOSS_BONUS
var target: Node2D
var vis: Node2D
var state: State = State.IDLE

var _state_t := 0.0
var _flash := 0.0
var _next := 0  # 0 = stomp, 1 = laser, alternating


func _ready() -> void:
	add_to_group("enemy")
	collision_layer = 1 << 2
	collision_mask = 1
	var shape := CollisionShape2D.new()
	var box := RectangleShape2D.new()
	box.size = Feel.MINIBOSS_SIZE
	shape.shape = box
	add_child(shape)
	vis = Node2D.new()
	add_child(vis)
	vis.add_child(Feel.make_sprite(Feel.TEX_MINIBOSS, Feel.MINIBOSS_SIZE * Feel.SPRITE_BLEED, Feel.COLOR_MINIBOSS))


func _physics_process(delta: float) -> void:
	if hp <= 0:
		return
	_state_t += delta
	if not is_on_floor():
		velocity.y = minf(velocity.y + Feel.GRAVITY * delta, Feel.MAX_FALL_SPEED)
	match state:
		State.IDLE:
			if _state_t >= _cycle():
				_begin_attack()
		State.WINDUP:
			if _state_t >= Feel.MINIBOSS_TELEGRAPH_TIME:
				_release_attack()
		State.RECOVER:
			if _state_t >= Feel.MINIBOSS_RECOVER_TIME:
				_enter(State.IDLE)
	_contact_kill()
	if _flash > 0.0:
		_flash -= delta
		vis.modulate = Feel.COLOR_FLASH
	elif phase == 2:
		vis.modulate = Feel.COLOR_MINIBOSS_PHASE2
	else:
		vis.modulate = Color.WHITE
	move_and_slide()


func take_hit(dmg: int) -> void:
	if hp <= 0:
		return
	hp -= dmg
	_flash = Feel.HIT_FLASH_TIME
	if phase == 1 and hp <= Feel.BOSS_ROBOT_PHASE2_AT_HP:
		phase = 2
		phase2_started.emit()
	if hp <= 0:
		_die()


func _cycle() -> float:
	return Feel.MINIBOSS_PHASE2_ATTACK_CYCLE if phase == 2 else Feel.MINIBOSS_ATTACK_CYCLE


func _enter(s: State) -> void:
	state = s
	_state_t = 0.0


func _begin_attack() -> void:
	_flash = Feel.MINIBOSS_TELEGRAPH_TIME  # telegraph glow while winding up
	_enter(State.WINDUP)


func _release_attack() -> void:
	if _next == 0:
		_stomp()
	else:
		_laser()
	_next = 1 - _next
	_enter(State.RECOVER)


func _side_to_target() -> int:
	if target == null or not is_instance_valid(target):
		return -1
	return -1 if target.global_position.x < global_position.x else 1


func _stomp() -> void:
	attacked.emit("stomp")
	var parent := get_parent()
	if parent == null:
		return
	var dir := _side_to_target()
	_spawn_wave(parent, dir)
	if phase == 2 and Feel.MINIBOSS_PHASE2_BOTH_WAVES:
		_spawn_wave(parent, -dir)


func _spawn_wave(parent: Node, dir: int) -> void:
	var w := Shockwave.new(dir)
	parent.add_child(w)
	var edge := Feel.MINIBOSS_SIZE.x * 0.5 + Feel.MINIBOSS_SHOCKWAVE_SIZE.x * 0.5
	w.position = global_position + Vector2(float(dir) * edge, 0.0)
	Feel.spawn_explosion(parent, w.position, Feel.MINIBOSS_SHOCKWAVE_SIZE * 1.4)


func _laser() -> void:
	attacked.emit("laser")
	var parent := get_parent()
	if parent == null:
		return
	var side := _side_to_target()
	var origin_x := global_position.x
	var length := origin_x - (Feel.ROOM_MIN_X - 40.0) if side < 0 else (Feel.ROOM_MAX_X + 40.0) - origin_x
	var beam := LaserSweep.new(side, origin_x, length)
	parent.add_child(beam)


func _contact_kill() -> void:
	if target == null or not is_instance_valid(target) or not target.has_method("hit"):
		return
	if target.get("dead") == true:
		return
	var d := target.global_position - global_position
	if absf(d.x) < (Feel.MINIBOSS_SIZE.x + Feel.PLAYER_SIZE.x) * 0.5 \
			and absf(d.y) < (Feel.MINIBOSS_SIZE.y + Feel.PLAYER_SIZE.y) * 0.5:
		target.hit()


func _die() -> void:
	Feel.spawn_explosion(get_parent(), global_position, Feel.MINIBOSS_SIZE * Feel.EXPLOSION_SCALE)
	died.emit(points)
	queue_free()


class Shockwave:
	extends Area2D
	## Stomp shockwave: a low floor skim travelling at Feel speed. Jump it;
	## buried heroes are beneath it (their collision layer drops out entirely).

	var dir := -1
	var kind := "shockwave"
	var _life := 0.0

	func _init(d: int) -> void:
		dir = d
		collision_layer = 0
		collision_mask = 1 << 1
		monitoring = true
		var cs := CollisionShape2D.new()
		var box := RectangleShape2D.new()
		box.size = Feel.MINIBOSS_SHOCKWAVE_SIZE
		cs.shape = box
		add_child(cs)
		var rect := ColorRect.new()
		rect.size = Feel.MINIBOSS_SHOCKWAVE_SIZE
		rect.position = -Feel.MINIBOSS_SHOCKWAVE_SIZE * 0.5
		rect.color = Feel.COLOR_SHOCKWAVE
		rect.mouse_filter = Control.MOUSE_FILTER_IGNORE
		add_child(rect)

	func _ready() -> void:
		add_to_group(Feel.HAZARD_GROUP)
		body_entered.connect(_on_body)
		position.y = Feel.GROUND_RECT.position.y - Feel.MINIBOSS_SHOCKWAVE_SIZE.y * 0.5

	func _physics_process(delta: float) -> void:
		position.x += float(dir) * Feel.MINIBOSS_SHOCKWAVE_SPEED * delta
		_life += delta
		if _life >= Feel.MINIBOSS_SHOCKWAVE_LIFE \
				or position.x < Feel.ROOM_MIN_X - 60.0 or position.x > Feel.ROOM_MAX_X + 60.0:
			queue_free()

	func _on_body(body: Node2D) -> void:
		if body.is_in_group("player") and body.has_method("hit"):
			body.hit()


class LaserSweep:
	extends Area2D
	## Laser sweep: a full-length beam from the robot's eye to the wall on the
	## aimed side, descending from LASER_HIGH_Y to LASER_LOW_Y over LASER_TIME,
	## then gone. High and harmless at first, floor-slamming at the end.

	var side := -1
	var kind := "laser"
	var h := Feel.MINIBOSS_LASER_HIGH_Y  # beam centre height above the floor line
	var _t := 0.0
	var _origin_x := 0.0
	var _length := 0.0

	func _init(s: int, origin_x: float, length: float) -> void:
		side = s
		_origin_x = origin_x
		_length = length
		collision_layer = 0
		collision_mask = 1 << 1
		monitoring = true
		var cs := CollisionShape2D.new()
		var box := RectangleShape2D.new()
		box.size = Vector2(_length, Feel.MINIBOSS_LASER_WIDTH)
		cs.shape = box
		add_child(cs)
		var rect := ColorRect.new()
		rect.size = Vector2(_length, Feel.MINIBOSS_LASER_WIDTH)
		rect.position = Vector2(-_length, -Feel.MINIBOSS_LASER_WIDTH) * 0.5
		rect.color = Feel.COLOR_LASER
		rect.mouse_filter = Control.MOUSE_FILTER_IGNORE
		add_child(rect)

	func _ready() -> void:
		add_to_group(Feel.HAZARD_GROUP)
		body_entered.connect(_on_body)
		position = Vector2(_origin_x + float(side) * _length * 0.5,
				Feel.GROUND_RECT.position.y - h)

	func _physics_process(delta: float) -> void:
		_t += delta
		h = lerpf(Feel.MINIBOSS_LASER_HIGH_Y, Feel.MINIBOSS_LASER_LOW_Y,
				clampf(_t / Feel.MINIBOSS_LASER_TIME, 0.0, 1.0))
		position.y = Feel.GROUND_RECT.position.y - h
		if _t >= Feel.MINIBOSS_LASER_TIME:
			queue_free()

	func _on_body(body: Node2D) -> void:
		if body.is_in_group("player") and body.has_method("hit"):
			body.hit()
