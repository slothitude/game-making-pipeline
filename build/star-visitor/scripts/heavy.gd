class_name HeavyAgent
extends EnemyAgent
## Roster v1 "agent_heavy" (spec: hp 3, speed 60, advance + burst-fire 3) — slice C.
## Same brain and signature-system hooks as the agent (mountable, scareable,
## drag-down-able, throwable); trades speed for a 3-round burst and staying power.

var _burst_left := 0


func _ready() -> void:
	hp = Feel.ENEMY_HEAVY_HP
	speed = Feel.ENEMY_HEAVY_SPEED
	super()
	for c in vis.get_children():
		c.queue_free()
	vis.add_child(Feel.make_sprite(Feel.TEX_ENEMY_HEAVY, Feel.ENEMY_SIZE * Feel.SPRITE_BLEED, Feel.COLOR_ENEMY_HEAVY))


func _enter(s: State) -> void:
	super(s)
	if s == State.SHOOT:
		_burst_left = Feel.ENEMY_HEAVY_BURST - 1  # the parent's _enter already fired round 1


func _do_shoot() -> void:
	velocity.x = 0.0
	if _burst_left > 0 and _state_t >= float(Feel.ENEMY_HEAVY_BURST - _burst_left) * Feel.ENEMY_HEAVY_BURST_GAP:
		_fire()
		_burst_left -= 1
	if _state_t >= Feel.ENEMY_SHOOT_TIME + float(Feel.ENEMY_HEAVY_BURST - 1) * Feel.ENEMY_HEAVY_BURST_GAP:
		_enter(State.ADVANCE)
