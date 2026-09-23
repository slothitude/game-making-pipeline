class_name Player
extends CharacterBody2D
## STAR VISITOR hero controller — milestone A:
## run / jump / duck, shoot-direction state machine, one-hit death, respawn iframes.

signal died(lives_left: int)
signal respawned

var lives: int = Feel.PLAYER_LIVES
var dead := false
var ducking := false
var facing := 1
var invulnerable := false
var spawn_point: Vector2 = Feel.SPAWN_POINT
var weapon: Weapon
var vis: Node2D
var ride: Ride
var burrow: Burrow
var style: StyleTracker = null
var life_spot: Burrow.LifeSpot = null

var _shape: CollisionShape2D
var _box: RectangleShape2D
var _iframes_left := 0.0
var _jump_queued := false
var _fire_prev := false
var _was_on_floor := false
var _squash_tw: Tween


func _ready() -> void:
	add_to_group("player")
	collision_layer = 1 << 1
	collision_mask = 1
	_box = RectangleShape2D.new()
	_box.size = Feel.PLAYER_SIZE
	_shape = CollisionShape2D.new()
	_shape.shape = _box
	add_child(_shape)
	vis = Node2D.new()
	add_child(vis)
	vis.add_child(Feel.make_sprite(Feel.TEX_HERO, Feel.PLAYER_SIZE * Feel.SPRITE_BLEED, Feel.COLOR_HERO))
	weapon = Weapon.new()
	weapon.setup(self)
	add_child(weapon)
	ride = Ride.new()
	ride.setup(self)
	add_child(ride)
	burrow = Burrow.new()
	burrow.setup(self)
	add_child(burrow)


func _physics_process(delta: float) -> void:
	if dead:
		return
	_tick_iframes(delta)

	# buried: the burrow system owns movement, drag-down, suffocation, surfacing
	if burrow.tick(delta):
		weapon.charging = false
		_apply_collider()
		return

	# mounted: the ride system owns the rider (bite / flip-and-throw / dismount)
	if ride.active:
		ride.tick(delta)
		_fire_prev = Input.is_action_pressed("fire")
		weapon.charging = false
		return

	_read_fire()

	var axis := Input.get_axis("move_left", "move_right")
	ducking = Feel.DUCK_ENABLED and is_on_floor() and Input.is_action_pressed("move_down")
	if ducking:
		axis = 0.0
	velocity.x = axis * Feel.RUN_SPEED
	if axis != 0.0:
		facing = 1 if axis > 0.0 else -1

	var jump_want := _jump_queued or Input.is_action_just_pressed("jump")
	_jump_queued = false
	if jump_want and not is_on_floor() and Input.is_action_pressed("move_down"):
		if ride.try_mount() != null:
			return
	if jump_want and is_on_floor():
		velocity.y = Feel.JUMP_VELOCITY
		_squash(Feel.SQUASH_JUMP_SCALE)

	if not is_on_floor():
		velocity.y = minf(velocity.y + Feel.GRAVITY * delta, Feel.MAX_FALL_SPEED)

	move_and_slide()

	if is_on_floor() and not _was_on_floor:
		_squash(Feel.SQUASH_LAND_SCALE)
	_was_on_floor = is_on_floor()

	_apply_collider()
	_apply_vis()


# -- input helpers (edge-triggered actions are also callable directly by tests/replays) --

func try_jump() -> void:
	_jump_queued = true


func aim_dir() -> Vector2:
	var axis := Input.get_axis("move_left", "move_right")
	if Input.is_action_pressed("move_up"):
		return Feel.AIM_FORWARD_UP if axis != 0.0 else Feel.AIM_UP
	if not is_on_floor() and Input.is_action_pressed("move_down"):
		return Feel.AIM_DOWN
	return Feel.AIM_FORWARD


func collider_height() -> float:
	return _box.size.y


func is_burrowed() -> bool:
	return burrow != null and burrow.burrowing


func add_style_kind(kind: String) -> void:
	if style != null:
		style.add(kind)


## Called by the ride system when the mount dies under the rider.
func on_mount_killed() -> void:
	if ride != null:
		ride.on_mount_killed()


func _read_fire() -> void:
	var held := Input.is_action_pressed("fire")
	if held and not _fire_prev:
		weapon.on_fire_pressed(aim_dir())
	elif not held and _fire_prev:
		weapon.on_fire_released(aim_dir())
	_fire_prev = held


func _tick_iframes(delta: float) -> void:
	if not invulnerable:
		return
	_iframes_left -= delta
	vis.modulate.a = 0.45 if fmod(_iframes_left, Feel.IFRAME_BLINK) < Feel.IFRAME_BLINK * 0.5 else 1.0
	if _iframes_left <= 0.0:
		invulnerable = false
		vis.modulate.a = 1.0


func _apply_collider() -> void:
	var h := Feel.PLAYER_DUCK_HEIGHT if ducking else Feel.PLAYER_SIZE.y
	if not is_equal_approx(_box.size.y, h):
		_box.size.y = h
		_shape.position.y = (Feel.PLAYER_SIZE.y - h) * 0.5


func _apply_vis() -> void:
	var running := _squash_tw != null and _squash_tw.is_valid() and _squash_tw.is_running()
	if not running:
		vis.scale = Vector2(float(facing), 1.0)


func _squash(s: Vector2) -> void:
	if _squash_tw != null and _squash_tw.is_valid():
		_squash_tw.kill()
	vis.scale = Vector2(s.x * float(facing), s.y)
	_squash_tw = create_tween()
	_squash_tw.tween_property(vis, "scale", Vector2(float(facing), 1.0), Feel.SQUASH_TIME)


# -- combat --

func hit(force := false) -> bool:
	# buried is invulnerable (bullets pass overhead); force = suffocation kill
	if dead or (burrow != null and burrow.burrowing and not force):
		return false
	if invulnerable and not force:
		return false
	if ride != null and ride.active:
		ride.end()  # dismount first, then normal death rules
	dead = true
	lives -= 1
	velocity = Vector2.ZERO
	ducking = false
	weapon.charging = false
	weapon.charge_t = 0.0
	vis.visible = false
	vis.modulate = Color.WHITE
	vis.position = Vector2.ZERO
	_shape.set_deferred("disabled", true)
	Feel.spawn_explosion(get_parent(), global_position, Feel.PLAYER_SIZE * Feel.EXPLOSION_SCALE)
	died.emit(lives)
	return true


func respawn(pos: Vector2) -> void:
	global_position = pos
	dead = false
	ducking = false
	velocity = Vector2.ZERO
	vis.visible = true
	vis.modulate = Color.WHITE
	vis.position = Vector2.ZERO
	collision_layer = 1 << 1
	_shape.set_deferred("disabled", false)
	invulnerable = true
	_iframes_left = Feel.RESPAWN_IFRAMES
	respawned.emit()
