class_name EscortOption
extends Node2D
## Milestone 3 escort option plane (spec powerups.escort_options): a small
## copy of the player plane that orbits the ship at a fixed radius and fires
## one parallel stream straight up whenever the main weapon fires.
## The PlayerPlane owns the orbit clock and the tier gating; an option only
## knows its phase offset and its gun.

const TEX_LEVEL := "res://assets/generated/player_p51.png"

var orbit_phase := 0.0   # rad offset around the orbit (options sit opposite)
var shots_fired := 0

var _sprite: Sprite2D
var _tex: Texture2D


func _init(phase := 0.0) -> void:
	orbit_phase = phase


func _ready() -> void:
	_sprite = Sprite2D.new()
	_sprite.scale = Vector2.ONE * (Feel.PLANE_SPRITE_SCALE * Feel.ESCORT_SCALE)
	add_child(_sprite)
	if ResourceLoader.exists(TEX_LEVEL):
		_tex = load(TEX_LEVEL)
	_sprite.texture = _tex  # null keeps the procedural fallback below


## Orbit position around the plane at orbit-clock time t.
func orbit_at(t: float) -> Vector2:
	var a := t * Feel.ESCORT_ORBIT_RAD_PER_SEC + orbit_phase
	return Vector2(cos(a), sin(a)) * Feel.ESCORT_ORBIT_RADIUS_PX


## One parallel shot from wherever the option currently is.
func fire(pool: BulletPool) -> void:
	if pool == null:
		return
	if pool.spawn(global_position, Vector2.UP * Feel.BULLET_SPEED,
			Feel.PLAYER_BULLET_DAMAGE, Feel.PLAYER_BULLET_RADIUS, true) != null:
		shots_fired += 1


func _draw() -> void:
	if _tex != null:
		return
	# original_assets (procedural law): never block on art.
	var r := Feel.PLANE_SPRITE_SCALE * Feel.ESCORT_SCALE * 260.0
	var pts := PackedVector2Array([
		Vector2(0.0, -r * 1.2),
		Vector2(r, r * 0.8),
		Vector2(0.0, r * 0.5),
		Vector2(-r, r * 0.8),
		Vector2(0.0, -r * 1.2),
	])
	draw_colored_polygon(pts, Color(0.82, 0.85, 0.9))
	draw_polyline(pts, Feel.HUD_OUTLINE_COLOR, 2.0)
