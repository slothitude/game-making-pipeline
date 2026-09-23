class_name Ride
extends Node
## Enemy ride (spec: signature_systems.enemy_ride) — milestone B.
## Down+Jump while airborne above an enemy mounts it. While mounted the mount
## flails blinded and soaks its faction's bullets (friendly fire). The rider can
## BITE (fire: scares enemies in a radius) or FLIP-AND-THROW (jump + direction:
## the mount becomes a projectile). Jump with no direction dismounts.

var active := false
var mount: EnemyAgent = null

var _player: Player = null
var _fire_prev := false
var _jump_prev := false


func setup(p: Player) -> void:
	_player = p


func can_mount() -> bool:
	if _player == null or active or _player.dead:
		return false
	if _player.is_on_floor() or _player.burrow.burrowing:
		return false
	if not Input.is_action_pressed("move_down"):
		return false
	return true


## Returns the enemy grabbed, or null. Call on the Down+Jump press while airborne.
func try_mount() -> EnemyAgent:
	if not can_mount():
		return null
	var best: EnemyAgent = null
	var best_d := INF
	for e in _player.get_tree().get_nodes_in_group("enemy"):
		var enemy := e as EnemyAgent
		if enemy == null or not enemy.can_be_mounted():
			continue
		var d := enemy.global_position - _player.global_position
		if absf(d.x) > Feel.RIDE_GRAB_RADIUS_X or d.y < -Feel.ENEMY_SIZE.y * 0.5:
			continue
		if absf(d.y) > Feel.RIDE_GRAB_RADIUS_Y:
			continue
		if d.length() < best_d:
			best_d = d.length()
			best = enemy
	if best == null:
		return null
	begin(best)
	return best


func begin(enemy: EnemyAgent) -> void:
	mount = enemy
	active = true
	enemy.begin_mount(_player)
	_player.velocity = Vector2.ZERO
	# sample live input so the press that triggered the mount can't double-fire
	_fire_prev = Input.is_action_pressed("fire")
	_jump_prev = Input.is_action_pressed("jump")
	if _player.style != null:
		_player.style.add("ride")


func tick(delta: float) -> void:
	if not active:
		return
	if mount == null or not is_instance_valid(mount) or mount.hp <= 0 or not mount.mounted:
		end()
		return
	# rider sticks to the mount's back
	_player.global_position = mount.global_position + Vector2(0.0, -_anchor_up())
	_player.velocity = Vector2.ZERO

	var fire := Input.is_action_pressed("fire")
	var jump := Input.is_action_pressed("jump")
	var dir := Input.get_axis("move_left", "move_right")

	var fire_pressed := fire and not _fire_prev
	var jump_pressed := jump and not _jump_prev
	_fire_prev = fire
	_jump_prev = jump

	if jump_pressed:
		if dir != 0.0:
			_throw(dir)
		else:
			_dismount(Feel.JUMP_VELOCITY * Feel.RIDE_DISMOUNT_HOP)
		return
	if fire_pressed:
		bite()


func bite() -> void:
	if not active or mount == null or not is_instance_valid(mount):
		return
	var arena := mount.get_parent()
	Feel.spawn_explosion(arena, mount.global_position, Feel.ENEMY_SIZE * 0.8)
	for e in mount.get_tree().get_nodes_in_group("enemy"):
		var enemy := e as EnemyAgent
		if enemy == null or enemy == mount or enemy.hp <= 0:
			continue
		if enemy.global_position.distance_to(mount.global_position) <= Feel.BITE_RADIUS:
			enemy.scare(mount.global_position)


func _throw(dir: float) -> void:
	var m := mount
	_dismount(Feel.JUMP_VELOCITY * Feel.RIDE_DISMOUNT_HOP)
	if m != null and is_instance_valid(m):
		m.throw_self(Vector2(dir, 0.0), _player.add_style_kind)


func _dismount(pop_velocity: float) -> void:
	end()
	if _player != null:
		_player.velocity = Vector2(0.0, pop_velocity)


func end() -> void:
	if mount != null and is_instance_valid(mount) and mount.mounted:
		mount.end_mount()
	mount = null
	active = false
	_fire_prev = false
	_jump_prev = false


## Called by the mount when it dies under the rider.
func on_mount_killed() -> void:
	mount = null
	active = false
	_fire_prev = Input.is_action_pressed("fire")
	_jump_prev = Input.is_action_pressed("jump")
	if _player != null:
		_player.velocity = Vector2(0.0, Feel.JUMP_VELOCITY * Feel.RIDE_DISMOUNT_HOP)


func _anchor_up() -> float:
	return Feel.ENEMY_SIZE.y * 0.5 + Feel.PLAYER_SIZE.y * 0.5
