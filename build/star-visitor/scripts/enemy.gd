class_name EnemyAgent
extends CharacterBody2D
## Roster v1 "agent": hp 1, advance + stop-and-shoot.
## State machine: ADVANCE -> STOP -> SHOOT (fires one bullet at the player) -> ADVANCE.
## Its bullet one-hits the player.
## Milestone B adds the shared ride states: MOUNTED (rider on board, flails
## semi-randomly), FLEEING (scared by a bite, runs away), THROWN (rider flipped
## it into a projectile that damages other enemies).

signal died(points: int)

enum State { ADVANCE, STOP, SHOOT, MOUNTED, FLEEING, THROWN }

var state: State = State.ADVANCE
var hp: int = Feel.ENEMY_HP
var speed: float = Feel.ENEMY_SPEED
var points: int = Feel.SCORE_PER_KILL
var target: Node2D
var bullet_parent: Node
var vis: Node2D
var mounted := false
var rider: Node2D = null
var style_cb := Callable()

var _state_t := 0.0
var _flash := 0.0
var _facing := 1
var _flail_dir := 1
var _throw_hits: Array = []


func _ready() -> void:
	add_to_group("enemy")
	collision_layer = 1 << 2
	collision_mask = 1
	var shape := CollisionShape2D.new()
	var box := RectangleShape2D.new()
	box.size = Feel.ENEMY_SIZE
	shape.shape = box
	add_child(shape)
	vis = Node2D.new()
	add_child(vis)
	vis.add_child(Feel.make_sprite(Feel.TEX_ENEMY_AGENT, Feel.ENEMY_SIZE * Feel.SPRITE_BLEED, Feel.COLOR_ENEMY))


func _physics_process(delta: float) -> void:
	if hp <= 0:
		return
	_state_t += delta
	if not is_on_floor() and state != State.THROWN:
		velocity.y = minf(velocity.y + Feel.GRAVITY * delta, Feel.MAX_FALL_SPEED)
	match state:
		State.ADVANCE:
			_do_advance()
		State.STOP:
			_do_stop()
		State.SHOOT:
			_do_shoot()
		State.MOUNTED:
			_do_flail()
		State.FLEEING:
			_do_flee()
		State.THROWN:
			_do_thrown(delta)
	if _flash > 0.0:
		_flash -= delta
		vis.modulate = Feel.COLOR_FLASH
	else:
		vis.modulate = Color.WHITE
	move_and_slide()
	if state == State.MOUNTED:
		global_position.x = clampf(global_position.x, Feel.ROOM_MIN_X, Feel.ROOM_MAX_X)
	vis.scale = Vector2(float(_face_dir()), 1.0)


func take_hit(dmg: int) -> void:
	if hp <= 0:
		return
	hp -= dmg
	_flash = Feel.HIT_FLASH_TIME
	if hp <= 0:
		_die()


# -- milestone B: ride hooks --

func can_be_mounted() -> bool:
	return hp > 0 and not mounted and state != State.THROWN


## Rider climbs on: the mount flails blinded and soaks its faction's bullets.
func begin_mount(by: Node2D) -> void:
	mounted = true
	rider = by
	# carry the player layer bit so enemy bullets (world|player mask) can hit the mount body
	collision_layer = (1 << 2) | (1 << 1)
	_flail_dir = 1 if randf() > 0.5 else -1
	_enter(State.MOUNTED)


func end_mount() -> void:
	mounted = false
	rider = null
	collision_layer = 1 << 2
	if state == State.MOUNTED:
		_enter(State.ADVANCE)


func scare(from_pos: Vector2) -> bool:
	if hp <= 0 or mounted or state == State.THROWN:
		return false
	if state == State.FLEEING:
		_state_t = 0.0
		return true
	_enter(State.FLEEING)
	_facing = -1 if from_pos.x > global_position.x else 1
	return true


## Flip-and-throw: the mount leaves as a projectile that damages other enemies.
func throw_self(dir: Vector2, cb: Callable) -> void:
	end_mount()
	style_cb = cb
	_throw_hits.clear()
	velocity = dir.normalized() * Feel.THROW_SPEED + Vector2(0.0, Feel.JUMP_VELOCITY * Feel.THROW_LOFT_MULT)
	_enter(State.THROWN)


# -- state machine --

func _do_advance() -> void:
	velocity.x = signf(_dx_to_target()) * speed
	if absf(_dx_to_target()) <= Feel.ENEMY_STOP_RANGE:
		_enter(State.STOP)


func _do_stop() -> void:
	velocity.x = 0.0
	if _state_t >= Feel.ENEMY_STOP_TIME:
		_enter(State.SHOOT)


func _do_shoot() -> void:
	velocity.x = 0.0
	if _state_t >= Feel.ENEMY_SHOOT_TIME:
		_enter(State.ADVANCE)


func _do_flail() -> void:
	# blinded wander: semi-random direction changes, hard-clamped to the room
	if _state_t >= Feel.RIDE_FLAIL_TURN_TIME and randf() < 0.5:
		_flail_dir = -_flail_dir
	velocity.x = _flail_dir * Feel.RIDE_FLAIL_SPEED
	if global_position.x <= Feel.ROOM_MIN_X:
		_flail_dir = 1
	elif global_position.x >= Feel.ROOM_MAX_X:
		_flail_dir = -1


func _do_flee() -> void:
	velocity.x = float(_facing) * speed * Feel.FLEE_SPEED_MULT
	if _state_t >= Feel.BITE_FLEE_TIME:
		_enter(State.ADVANCE)


func _do_thrown(delta: float) -> void:
	velocity.y = minf(velocity.y + Feel.GRAVITY * delta, Feel.MAX_FALL_SPEED)
	for other in get_tree().get_nodes_in_group("enemy"):
		if other == self or not is_instance_valid(other) or other.hp <= 0:
			continue
		if _throw_hits.has(other):
			continue
		if global_position.distance_to(other.global_position) <= Feel.ENEMY_SIZE.x:
			_throw_hits.append(other)
			other.take_hit(Feel.THROW_DAMAGE)
			if style_cb.is_valid():
				style_cb.call("throw_hit")
	# floor grace: the throw starts from a standing contact, so ignore the first instant
	if _state_t >= Feel.THROW_FLOOR_GRACE and (_state_t >= Feel.THROW_LIFE or is_on_wall() or is_on_floor()):
		_die()


func _enter(s: State) -> void:
	state = s
	_state_t = 0.0
	if s == State.SHOOT:
		_fire()


func _dx_to_target() -> float:
	if target == null or not is_instance_valid(target):
		return 0.0
	return target.global_position.x - global_position.x


func _face_dir() -> int:
	if state == State.FLEEING:
		return _facing
	if state == State.MOUNTED:
		return _facing
	var dx := _dx_to_target()
	if dx < 0.0:
		_facing = -1
	elif dx > 0.0:
		_facing = 1
	return _facing


func _fire() -> void:
	if target == null or not is_instance_valid(target):
		return
	var parent := bullet_parent if bullet_parent != null else get_parent()
	if parent == null:
		return
	var b := Bullet.new()
	parent.add_child(b)
	var from := global_position + Vector2(0.0, -Feel.ENEMY_SIZE.y * 0.25)
	var aim := (target.global_position - from).normalized()
	b.launch(from, aim, Feel.ENEMY_BULLET_SPEED, Feel.ENEMY_BULLET_DAMAGE, false, 0)


func _die() -> void:
	Feel.spawn_explosion(get_parent(), global_position, Feel.ENEMY_SIZE * Feel.EXPLOSION_SCALE)
	if mounted and rider != null and is_instance_valid(rider):
		if rider.has_method("on_mount_killed"):
			rider.on_mount_killed()
	died.emit(points)
	queue_free()
