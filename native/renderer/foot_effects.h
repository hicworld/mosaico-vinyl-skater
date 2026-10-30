#pragma once

#include <stdbool.h>
#include <stdint.h>

/* Persisted values: append new effects without reassigning an existing ID. */
typedef enum {
    FOOT_EFFECT_AUTO = 0,
    FOOT_EFFECT_OFF = 1,
    FOOT_EFFECT_LIGHTNING = 2,
    FOOT_EFFECT_SPARKS = 3,
    FOOT_EFFECT_CRYSTALS = 4,
    FOOT_EFFECT_ORBITS = 5,
    FOOT_EFFECT_FLAME = 6,
    FOOT_EFFECT_COUNT = 7
} foot_effect_mode_t;

typedef struct {
    /* x/y follows the visible shoe, including airborne height. */
    float x, y, ground_x, ground_y, z_world, load;
    float velocity_x, velocity_y; /* Screen pixels per second. */
    bool contact;
} foot_effect_foot_t;

typedef struct {
    uint64_t samples; /* The shared 16 kHz PCM clock. */
    float rms, drum_pulse, beat_phase;
    foot_effect_foot_t feet[2];
} foot_effect_context_t;

typedef void (*foot_effect_plot_t)(void *user, int x, int y,
                                   uint16_t rgb565, unsigned alpha);

/* AUTO: classic -> sparks, ivory -> crystals, lavender -> orbits, Neon -> electricity. */
unsigned foot_effect_resolve(unsigned mode, unsigned skin);

/* Layer 0: contact-only ground accents, before the skater.
 * Layer 1: moving shoe accents, after the skater. No state is retained between calls. */
void foot_effect_draw(const foot_effect_context_t *context, unsigned mode, unsigned layer,
                      foot_effect_plot_t plot, void *user);

/* Draw one real touchdown event. Caller supplies age in seconds and stable event seed.
 * Existing scene splash particles and diagnostics are independent of this accent. */
void foot_effect_draw_landing(const foot_effect_context_t *context, unsigned mode,
                              float x, float y, float age, float strength, uint32_t seed,
                              foot_effect_plot_t plot, void *user);
