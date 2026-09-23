class_name Weapon
extends Node
## Pistol: infinite ammo, on-screen bullet cap, charge shot.
## The cap is hard: when BULLET_CAP bullets are live, the next press does nothing.

var charging := false
var charge_t := 0.0

var _pool: Array[Bullet] = []
var _owner_body: Node2D


func setup(owner_body: Node2D) -> void:
	_owner_body = owner_body


func _physics_process(delta: float) -> void:
	if charging:
		charge_t += delta


func live_count() -> int:
	var n := 0
	for b in _pool:
		if is_instance_valid(b) and b.active:
			n += 1
	return n


func pool() -> Array[Bullet]:
	return _pool


func on_fire_pressed(dir: Vector2) -> bool:
	charge_t = 0.0
	charging = Feel.CHARGEABLE
	return _spawn(dir, false)


func on_fire_released(dir: Vector2) -> bool:
	charging = false
	var big := Feel.CHARGEABLE and charge_t >= Feel.CHARGE_TIME
	charge_t = 0.0
	if not big:
		return false
	return _spawn(dir, true)


func _spawn(dir: Vector2, big: bool) -> bool:
	if _owner_body == null:
		return false
	if live_count() >= Feel.BULLET_CAP:
		return false
	var b := _obtain()
	if not b.is_inside_tree():
		add_child(b)
	var mult := Feel.CHARGE_DAMAGE_MULT if big else 1
	var muzzle := _owner_body.global_position + dir * Feel.MUZZLE_DISTANCE
	b.launch(muzzle, dir, Feel.BULLET_SPEED, Feel.BULLET_DAMAGE * mult, true,
			Feel.CHARGE_PIERCE if big else 0)
	return true


func _obtain() -> Bullet:
	for b in _pool:
		if is_instance_valid(b) and not b.active:
			return b
	var b := Bullet.new()
	_pool.append(b)
	return b
