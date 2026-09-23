class_name Feel
extends RefCounted
## Single source of truth for every movement/combat number (spec law: constants_not_magic).
## Values mirror spec/study_spec.json. No gameplay code may hardcode numbers.

# -- movement (spec: movement) --
const RUN_SPEED: float = 260.0
const JUMP_VELOCITY: float = -420.0
const GRAVITY: float = 1100.0
const MAX_FALL_SPEED: float = 900.0
const DUCK_ENABLED: bool = true

# -- player (spec: player) --
const PLAYER_LIVES: int = 10
const PLAYER_ONE_HIT_DEATH: bool = true
const RESPAWN_IFRAMES: float = 1.5
const RESPAWN_DELAY: float = 0.8
const PLAYER_SIZE: Vector2 = Vector2(36, 48)
const PLAYER_DUCK_HEIGHT: float = 24.0
const SPAWN_POINT: Vector2 = Vector2(200, 400)

# -- aim directions (spec: shoot_directions_grounded / _airborne) --
const AIM_FORWARD: Vector2 = Vector2(1, 0)
const AIM_UP: Vector2 = Vector2(0, -1)
const AIM_FORWARD_UP: Vector2 = Vector2(0.7071067811865476, -0.7071067811865476)
const AIM_DOWN: Vector2 = Vector2(0, 1)

# -- pistol (spec: weapons.pistol) --
const AMMO_INFINITE: bool = true
const BULLET_CAP: int = 3
const BULLET_SPEED: float = 620.0
const BULLET_DAMAGE: int = 1
const BULLET_MAX_LIFE: float = 2.0
const BULLET_SIZE: Vector2 = Vector2(16, 7)
const MUZZLE_DISTANCE: float = 26.0
const CHARGEABLE: bool = true
const CHARGE_TIME: float = 0.8
const CHARGE_DAMAGE_MULT: int = 2
const CHARGE_PIERCE: int = 2

# -- ammo_types.fire pickup (spec; milestone B) --
const FIRE_BONUS_DAMAGE: int = 1
const FIRE_DURATION_HITS: int = 12

# -- grenades (spec; milestone B) --
const GRENADE_START_COUNT: int = 3
const GRENADE_THROW_VELOCITY: Vector2 = Vector2(180, -320)

# -- enemies (spec: enemies.roster_v1) --
const ENEMY_HP: int = 1
const ENEMY_SPEED: float = 90.0
const ENEMY_STOP_RANGE: float = 120.0
const ENEMY_STOP_TIME: float = 0.35
const ENEMY_SHOOT_TIME: float = 0.4
const ENEMY_BULLET_SPEED: float = 300.0
const ENEMY_BULLET_DAMAGE: int = 1
const ENEMY_BULLET_ONE_HITS_PLAYER: bool = true
const ENEMY_SIZE: Vector2 = Vector2(34, 46)
const ENEMY_HEAVY_HP: int = 3
const ENEMY_HEAVY_SPEED: float = 60.0

# -- bosses (spec: bosses_v1; milestones C/D) --
const BOSS_ROBOT_HP: int = 30
const BOSS_ROBOT_PHASE2_AT_HP: int = 15
const BOSS_VEHICLE_HP: int = 60
const BOSS_VEHICLE_PHASE2_AT_HP: int = 25

# -- scoring (spec: scoring) --
const SCORE_PER_KILL: int = 100
const BOSS_BONUS: int = 2000
const STYLE_PER_LIFE: int = 5000

# -- signature systems (spec; milestone B) --
const BURROW_SUFFOCATE_TIME: float = 4.0

# -- enemy ride (spec: signature_systems.enemy_ride; milestone B) --
const RIDE_GRAB_RADIUS_X: float = 52.0
const RIDE_GRAB_RADIUS_Y: float = 120.0
const RIDE_FLAIL_SPEED: float = 150.0
const RIDE_FLAIL_TURN_TIME: float = 0.45
const RIDE_DISMOUNT_HOP: float = 0.5
const THROW_SPEED: float = 520.0
const THROW_LOFT_MULT: float = 0.5
const THROW_FLOOR_GRACE: float = 0.08
const THROW_DAMAGE: int = 2
const THROW_LIFE: float = 1.2
const BITE_RADIUS: float = 190.0
const BITE_FLEE_TIME: float = 2.5
const FLEE_SPEED_MULT: float = 1.6

# -- burrow (spec: signature_systems.burrow; milestone B) --
const BURROW_ENTER_TIME: float = 0.6
const BURROW_MOVE_SPEED: float = 120.0
const BURROW_DRAG_RADIUS: float = 44.0
const BURROW_WARN_AT: float = 3.0
const BURROW_SINK: float = 14.0

# -- style stub (spec: scoring.style_bonus; milestone B) --
const STYLE_RIDE: int = 150
const STYLE_THROW_HIT: int = 200
const STYLE_BURROW_KILL: int = 250

# -- milestone B room constants --
const ROOM_MIN_X: float = 24.0
const ROOM_MAX_X: float = 936.0
const LIFE_SPOT_RECT: Rect2 = Rect2(470, 440, 60, 60)
const WAVE2_AGENT_COUNT: int = 6
const WAVE2_DELAY: float = 1.2
const EXTRA_PLATFORM: Rect2 = Rect2(430, 200, 160, 16)

# -- wave 1 (spec: beat_sheet_v1 "wave1 agents x6") --
const WAVE1_AGENT_COUNT: int = 6
const WAVE_SPAWN_INTERVAL: float = 0.8
const WAVE_SPAWN_X: float = 920.0
const WAVE_SPAWN_Y: float = 200.0
const WAVE_SPAWN_SPACING: float = 44.0

# -- slice C: heavy enemy (spec: enemies.roster_v1 "agent_heavy") --
const ENEMY_HEAVY_BURST: int = 3
const ENEMY_HEAVY_BURST_GAP: float = 0.22
const COLOR_ENEMY_HEAVY: Color = Color(0.62, 0.36, 0.3)

# -- slice C: miniboss robot (spec: bosses_v1.miniboss_robot) --
const MINIBOSS_SIZE: Vector2 = Vector2(96, 132)
const MINIBOSS_DELAY: float = 1.2
const MINIBOSS_ATTACK_CYCLE: float = 2.4
const MINIBOSS_PHASE2_ATTACK_CYCLE: float = 1.4
const MINIBOSS_PHASE2_BOTH_WAVES: bool = true
const MINIBOSS_TELEGRAPH_TIME: float = 0.55
const MINIBOSS_RECOVER_TIME: float = 0.5
const MINIBOSS_SHOCKWAVE_SPEED: float = 300.0
const MINIBOSS_SHOCKWAVE_SIZE: Vector2 = Vector2(26, 18)
const MINIBOSS_SHOCKWAVE_LIFE: float = 4.0
const MINIBOSS_LASER_TIME: float = 1.15
const MINIBOSS_LASER_WIDTH: float = 18.0
const MINIBOSS_LASER_HIGH_Y: float = 150.0
const MINIBOSS_LASER_LOW_Y: float = 14.0
const COLOR_MINIBOSS: Color = Color(0.55, 0.58, 0.66)
const COLOR_MINIBOSS_PHASE2: Color = Color(1.0, 0.55, 0.55)
const COLOR_LASER: Color = Color(1.0, 0.25, 0.3, 0.85)
const COLOR_SHOCKWAVE: Color = Color(0.95, 0.75, 0.3)
const HAZARD_GROUP := "hazard"

# -- slice C: wave 2 goes mixed (spec beat_sheet_v1 "wave2 agents+heavies x10") --
const WAVE2_HEAVY_COUNT: int = 4
const WAVE2_TOTAL: int = WAVE2_AGENT_COUNT + WAVE2_HEAVY_COUNT

# -- test room geometry (slice A level layout) --
const GROUND_RECT: Rect2 = Rect2(0, 480, 960, 60)
const PLATFORMS: Array[Rect2] = [Rect2(250, 390, 180, 16), Rect2(610, 300, 180, 16)]
const WALL_LEFT_RECT: Rect2 = Rect2(-40, 0, 40, 540)
const WALL_RIGHT_RECT: Rect2 = Rect2(960, 0, 40, 540)

# -- game feel (spec art law: static sprites + programmatic squash/stretch) --
const SPRITE_BLEED: float = 1.15
const SQUASH_JUMP_SCALE: Vector2 = Vector2(0.72, 1.28)
const SQUASH_LAND_SCALE: Vector2 = Vector2(1.28, 0.72)
const SQUASH_TIME: float = 0.12
const HIT_FLASH_TIME: float = 0.08
const DEATH_FX_TIME: float = 0.25
const IFRAME_BLINK: float = 0.15
const EXPLOSION_SCALE: float = 1.6

# -- fallback colors (original_assets law: code-drawn placeholder when art missing) --
const COLOR_HERO: Color = Color(0.25, 0.55, 0.95)
const COLOR_ENEMY: Color = Color(0.9, 0.3, 0.25)
const COLOR_FLASH: Color = Color(1.0, 0.35, 0.35)
const COLOR_BULLET: Color = Color(1.0, 0.9, 0.3)
const COLOR_WORLD: Color = Color(0.16, 0.14, 0.2)
const COLOR_GROUND: Color = Color(0.32, 0.28, 0.4)
const COLOR_PLATFORM: Color = Color(0.42, 0.36, 0.52)

# -- sprite paths (original assets law: generated art, keyed transparent) --
const TEX_HERO := "res://assets/generated/hero_idle.png"
const TEX_ENEMY_AGENT := "res://assets/generated/enemy_agent.png"
const TEX_ENEMY_HEAVY := "res://assets/generated/enemy_heavy.png"
const TEX_MINIBOSS := "res://assets/generated/miniboss_robot.png"
const TEX_BULLET := "res://assets/generated/bullet.png"
const TEX_EXPLOSION := "res://assets/generated/explosion.png"
const TEX_TITLE_LOGO := "res://assets/generated/title_logo.png"


static func make_sprite(tex_path: String, target_size: Vector2, fallback_color: Color) -> Sprite2D:
	# Returns a Sprite2D scaled so the art covers target_size; falls back to a
	# flat code-drawn rectangle if the generated art is missing (never block on art).
	var spr := Sprite2D.new()
	var tex: Texture2D = null
	if ResourceLoader.exists(tex_path):
		tex = load(tex_path)
	if tex == null:
		var img := Image.create(8, 8, false, Image.FORMAT_RGBA8)
		img.fill(fallback_color)
		tex = ImageTexture.create_from_image(img)
	spr.texture = tex
	var ts := tex.get_size()
	if ts.x > 0.0 and ts.y > 0.0:
		spr.scale = target_size / ts
	return spr


static func spawn_explosion(parent: Node, pos: Vector2, size: Vector2) -> void:
	if parent == null:
		return
	var fx := make_sprite(TEX_EXPLOSION, size, COLOR_BULLET)
	parent.add_child(fx)
	fx.global_position = pos
	var tw := fx.create_tween()
	tw.tween_property(fx, "scale", fx.scale * 1.6, DEATH_FX_TIME)
	tw.parallel().tween_property(fx, "modulate:a", 0.0, DEATH_FX_TIME)
	tw.tween_callback(fx.queue_free)
