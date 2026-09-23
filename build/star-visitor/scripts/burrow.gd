class_name Burrow
extends Node
## Burrow (spec: signature_systems.burrow) — milestone B.
## Hold Down on the ground long enough and the hero digs in: invulnerable,
## enemy bullets pass overhead, slow horizontal movement underground, and any
## enemy standing on top gets dragged down (instant kill). Stay under too long
## and suffocation kills the hero (warning flash in the last second).
## One buried spot in the test room hides an extra life.

signal warned
signal surfaced
signal life_found

var burrowing := false
var t := 0.0
var warning_active := false

var _player: Player = null
var _down_t := 0.0
var _warned := false
var _sinking := 0.0


func setup(p: Player) -> void:
	_player = p


## Call every physics frame BEFORE normal player physics. True = burrow handled it.
func tick(delta: float) -> bool:
	if _player == null or _player.dead:
		return burrowing
	if not burrowing:
		return _tick_enter(delta)
	_t(delta)
	return true


func _tick_enter(delta: float) -> bool:
	# dig in: Down held on the floor (past the duck window), not mounted
	if _player.ride.active or not _player.is_on_floor() or _player.ducking == false:
		_down_t = 0.0
		return false
	_down_t += delta
	if _down_t >= Feel.BURROW_ENTER_TIME:
		enter()
		return true
	return false


func enter() -> void:
	burrowing = true
	t = 0.0
	warning_active = false
	_warned = false
	_sinking = 0.0
	_player.ducking = false
	_player.velocity = Vector2.ZERO
	_player.collision_layer = 0  # bullets and bodies pass the hidden hero
	_player.vis.modulate.a = 0.35


func exit() -> void:
	if not burrowing:
		return
	burrowing = false
	t = 0.0
	_down_t = 0.0
	warning_active = false
	_player.collision_layer = 1 << 1
	_player.vis.modulate.a = 1.0
	_player.vis.position = Vector2.ZERO
	surfaced.emit()


func cancel_dig_charge() -> void:
	_down_t = 0.0


func _t(delta: float) -> void:
	t += delta
	# slow underground shuffle
	var axis := Input.get_axis("move_left", "move_right")
	_player.velocity = Vector2(axis * Feel.BURROW_MOVE_SPEED, 0.0)
	if axis != 0.0:
		_player.facing = 1 if axis > 0.0 else -1
	_player.move_and_slide()
	_player.global_position.x = clampf(_player.global_position.x, Feel.ROOM_MIN_X, Feel.ROOM_MAX_X)

	# sink the sprite as the timer runs
	_sinking = minf(_sinking + Feel.BURROW_SINK * delta * 2.0, Feel.BURROW_SINK)
	_player.vis.position.y = _sinking
	_player.vis.modulate.a = 0.35

	# drag down any enemy standing on top of the hidden hero
	_drag_below()

	# buried extra-life spot (one fixed spot, v1)
	if _player.life_spot != null and try_life_spot(_player.life_spot):
		_player.lives += 1
		life_found.emit()

	# suffocation
	if not warning_active and t >= Feel.BURROW_WARN_AT:
		warning_active = true
		warned.emit()
	if warning_active:
		_player.vis.modulate = Feel.COLOR_FLASH if fmod(t, 0.2) < 0.1 else Color(1, 1, 1, 0.35)
	if t >= Feel.BURROW_SUFFOCATE_TIME:
		exit()
		_player.hit(true)
		return
	# surface on Down release
	if not Input.is_action_pressed("move_down"):
		exit()


func _drag_below() -> void:
	for e in _player.get_tree().get_nodes_in_group("enemy"):
		var enemy := e as EnemyAgent
		if enemy == null or enemy.hp <= 0 or enemy.mounted or enemy.state == EnemyAgent.State.THROWN:
			continue
		var d := enemy.global_position - _player.global_position
		if absf(d.x) <= Feel.BURROW_DRAG_RADIUS and absf(d.y) <= Feel.ENEMY_SIZE.y * 0.6 \
				and enemy.is_on_floor():
			enemy.take_hit(999)
			if _player.style != null:
				_player.style.add("burrow_kill")
			Feel.spawn_explosion(_player.get_parent(), enemy.global_position, Feel.ENEMY_SIZE)


## Buried pickup: returns true the first time the buried hero reaches the spot.
func try_life_spot(spot: LifeSpot) -> bool:
	if not burrowing or spot == null or not is_instance_valid(spot) or spot.taken:
		return false
	var c := Feel.LIFE_SPOT_RECT.get_center()
	if absf(_player.global_position.x - c.x) <= Feel.LIFE_SPOT_RECT.size.x * 0.5 \
			and absf(_player.global_position.y - c.y) <= Feel.LIFE_SPOT_RECT.size.y * 0.5:
		return spot.take()
	return false


class LifeSpot:
	extends Node2D
	## One fixed buried extra-life spot (milestone B v1).
	signal found

	var taken := false

	func take() -> bool:
		if taken:
			return false
		taken = true
		found.emit()
		queue_free()
		return true
