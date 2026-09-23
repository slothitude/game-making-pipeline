class_name BulletPool
extends Node2D
## Pooled bullets (spec law via milestone 2 scope: POOLED bullets). Two live
## instances: player bullets (bullet_player.png) and enemy bullets
## (bullet_enemy.png). Fully stepped by hand (no physics) so tests and replays
## are deterministic. spawn() returns null once the cap is reached.

const TEX_PLAYER := "res://assets/generated/bullet_player.png"
const TEX_ENEMY := "res://assets/generated/bullet_enemy.png"

var cap := 0
var spawned_total := 0

var _bullets: Array[Bullet] = []
var _tex: Texture2D
var _scale := 1.0


class Bullet:
	extends Sprite2D
	var velocity := Vector2.ZERO
	var gravity := 0.0               # px/s^2 downward pull (lobbed missiles); 0 = straight
	var radius := 8.0
	var damage := 1
	var friendly := true
	var active := false


func setup(friendly: bool, pool_cap: int) -> void:
	cap = pool_cap
	var path := TEX_PLAYER if friendly else TEX_ENEMY
	if ResourceLoader.exists(path):
		_tex = load(path)
	_scale = Feel.PLAYER_BULLET_SCALE if friendly else Feel.ENEMY_BULLET_SCALE


## Reuse a dead bullet, grow the pool under the cap, or give up (returns null).
## gravity (px/s^2, positive falls) is optional; straight-line shots omit it.
func spawn(pos: Vector2, vel: Vector2, damage: int, radius: float, friendly: bool = true,
		gravity: float = 0.0) -> Bullet:
	var b := _find_inactive()
	if b == null:
		if _bullets.size() >= cap:
			return null
		b = Bullet.new()
		b.texture = _tex
		b.scale = Vector2.ONE * _scale
		add_child(b)
		_bullets.append(b)
	b.position = pos
	b.velocity = vel
	b.gravity = gravity
	b.damage = damage
	b.radius = radius
	b.friendly = friendly
	b.active = true
	b.visible = true
	b.rotation = vel.angle() + PI * 0.5  # bullet art points up
	spawned_total += 1
	return b


## Move everything; deactivate bullets that leave the screen. Returns count of
## live bullets afterwards (also the cheap way for tests to count volleys).
## Take one bullet out of play (hits, bomb clears, off-screen).
func retire(b: Bullet) -> void:
	b.active = false
	b.visible = false
	b.velocity = Vector2.ZERO


func step(delta: float) -> int:
	var view := get_viewport_rect().size
	var m := Feel.BULLET_OFFSCREEN_MARGIN_PX
	var live := 0
	for b in _bullets:
		if not b.active:
			continue
		if b.gravity != 0.0:
			b.velocity.y += b.gravity * delta
		b.position += b.velocity * delta
		if b.position.y < -m or b.position.y > view.y + m \
				or b.position.x < -m or b.position.x > view.x + m:
			retire(b)
		else:
			live += 1
	return live


func active_count() -> int:
	var n := 0
	for b in _bullets:
		if b.active:
			n += 1
	return n


func live_bullets() -> Array[Bullet]:
	var out: Array[Bullet] = []
	for b in _bullets:
		if b.active:
			out.append(b)
	return out


## Bomb support: retire every enemy bullet on screen. Returns how many died.
func clear_all() -> int:
	var n := 0
	for b in _bullets:
		if b.active:
			retire(b)
			n += 1
	return n


func reset() -> void:
	clear_all()
	spawned_total = 0


func _find_inactive() -> Bullet:
	for b in _bullets:
		if not b.active:
			return b
	return null
