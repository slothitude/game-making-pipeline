class_name Bullet
extends Area2D
## Pooled projectile shared by the pistol (capped) and agents (uncapped).
## Inactive bullets stay parked in the tree, hidden, ready for reuse.

var dir := Vector2(1, 0)
var speed := 0.0
var damage := 0
var pierce_left := 0
var from_player := true
var active := false
var _life := 0.0


func _init() -> void:
	visible = false
	monitoring = true
	collision_layer = 0
	collision_mask = 0
	set_physics_process(false)
	add_to_group("bullet")
	var cs := CollisionShape2D.new()
	var box := RectangleShape2D.new()
	box.size = Feel.BULLET_SIZE
	cs.shape = box
	add_child(cs)
	add_child(Feel.make_sprite(Feel.TEX_BULLET, Feel.BULLET_SIZE, Feel.COLOR_BULLET))


func _ready() -> void:
	body_entered.connect(_on_body_entered)


func launch(pos: Vector2, direction: Vector2, spd: float, dmg: int, player_owned: bool, pierce: int = 0) -> void:
	global_position = pos
	dir = direction.normalized()
	speed = spd
	damage = dmg
	pierce_left = pierce
	from_player = player_owned
	_life = 0.0
	active = true
	visible = true
	rotation = dir.angle()
	if from_player:
		collision_layer = 1 << 3
		collision_mask = 1 | (1 << 2)
	else:
		collision_layer = 1 << 4
		collision_mask = 1 | (1 << 1)
	set_physics_process(true)


func _physics_process(delta: float) -> void:
	if not active:
		return
	global_position += dir * speed * delta
	_life += delta
	if _life >= Feel.BULLET_MAX_LIFE:
		despawn()


func _on_body_entered(body: Node2D) -> void:
	if not active:
		return
	if body.is_in_group("world"):
		despawn()
		return
	if from_player and body.is_in_group("enemy"):
		if body.has_method("take_hit"):
			body.take_hit(damage)
		pierce_left -= 1
		if pierce_left < 0:
			despawn()
	elif not from_player and body.is_in_group("enemy") and body.get("mounted") == true:
		# friendly fire: a mounted mount soaks its own faction's bullets
		if body.has_method("take_hit"):
			body.take_hit(damage)
		despawn()
	elif not from_player and body.is_in_group("player"):
		if body.is_burrowed():
			return  # buried: bullets pass overhead, no hit, no despawn
		if body.has_method("hit"):
			body.hit()
		despawn()


func despawn() -> void:
	active = false
	visible = false
	set_physics_process(false)
