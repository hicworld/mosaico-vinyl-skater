#include "adaptive_scene.h"
#include "adaptive_art.h"
#include "record_colors.h"
#include "nature_art.h"
#include "art_codec.h"
#include "song_format.h"
#include <math.h>
#include <string.h>
#define INPUT_CAPACITY (64u*1024*1024)
#define STRIDE 488u
static uint8_t file_bytes[INPUT_CAPACITY];
static uint16_t guarded_canvas[480*STRIDE+32],guarded_background[480*480+32];
static uint8_t guarded_scratch[SCENE_SCRATCH_BYTES+32],guarded_alpha[480*480+32];
static adaptive_scene_t scene;
static motion_timeline_t timeline;
static song_header_t header;
static motion_pose_t pose;
static float feet[8];
static float effects[12];
static int loaded;
int input_ptr(void){return (int)file_bytes;}
int load_package(unsigned bytes){
 loaded=0;
 if(bytes>INPUT_CAPACITY||!song_format_header(file_bytes,bytes,bytes,&header))return 0;
 const song_beat_t *beats=(const song_beat_t*)(file_bytes+header.beats_offset);
 const song_plan_t *plans=(const song_plan_t*)(file_bytes+header.plans_offset);
 if(!song_format_metadata(&header,beats,plans)||song_crc32(0,file_bytes+128,bytes-128)!=header.payload_crc32)return 0;
 unsigned metadata_end=header.plans_offset+header.plan_count*sizeof(song_plan_t);
 song_audio_features_t audio;
 if(!song_format_audio(&header,file_bytes+metadata_end,header.pcm_offset-metadata_end,&audio))return 0;
 timeline=(motion_timeline_t){.sample_count=header.sample_count,.beat_count=header.beat_count,
     .plan_count=header.plan_count,.beats=beats,.plans=plans,.audio=audio,.theme=song_format_theme(&header),.record_id=song_format_record(&header),.effect_id=song_format_effect(&header)};
 scene.last_samples=UINT64_MAX;loaded=1;return 1;
}
int init_scene(void){
 for(unsigned i=0;i<sizeof(guarded_canvas)/sizeof(*guarded_canvas);i++)guarded_canvas[i]=0xa55a;
 for(unsigned i=0;i<sizeof(guarded_background)/sizeof(*guarded_background);i++)guarded_background[i]=0xa55a;
 memset(guarded_scratch,0xa5,sizeof(guarded_scratch));memset(guarded_alpha,0xa5,sizeof(guarded_alpha));
 return adaptive_scene_init(&scene,guarded_canvas+16,STRIDE,guarded_background+16,guarded_scratch+16,SCENE_SCRATCH_BYTES,guarded_alpha+16);
}
int render_frame(unsigned long long samples,unsigned skin,unsigned song,unsigned playing){
 return loaded&&adaptive_scene_render(&scene,&timeline,samples,skin,song,playing!=0)?(int)(guarded_canvas+16):0;
}
int render_frame_theme(unsigned long long samples,unsigned skin,unsigned song,unsigned playing,unsigned theme){
 if(!loaded||theme>=SONG_THEME_COUNT)return 0;
 motion_timeline_t selected=timeline;selected.theme=theme;
 return adaptive_scene_render(&scene,&selected,samples,skin,song,playing!=0)?(int)(guarded_canvas+16):0;
}
unsigned get_package_theme(void){return loaded?timeline.theme:SONG_THEME_COUNT;}
unsigned current_theme(void){return scene.last_theme;}
int render_frame_style(unsigned long long samples,unsigned skin,unsigned song,unsigned playing,unsigned theme,unsigned record_id){
 if(!loaded||theme>=SONG_THEME_COUNT||record_id>=SCENE_RECORD_COLOR_COUNT)return 0;
 motion_timeline_t selected=timeline;selected.theme=theme;selected.record_id=record_id;
 return adaptive_scene_render(&scene,&selected,samples,skin,song,playing!=0)?(int)(guarded_canvas+16):0;
}
unsigned get_package_record(void){return loaded?timeline.record_id:SONG_RECORD_COLOR_COUNT;}
unsigned current_record(void){return scene.last_record_id;}
int render_frame_effect(unsigned long long samples,unsigned skin,unsigned song,unsigned playing,unsigned theme,unsigned record_id,unsigned effect_id){
 if(!loaded||theme>=SONG_THEME_COUNT||record_id>=SCENE_RECORD_COLOR_COUNT||effect_id>=SONG_EFFECT_COUNT)return 0;
 motion_timeline_t selected=timeline;selected.theme=theme;selected.record_id=record_id;selected.effect_id=effect_id;
 return adaptive_scene_render(&scene,&selected,samples,skin,song,playing!=0)?(int)(guarded_canvas+16):0;
}
unsigned get_package_effect(void){return loaded?timeline.effect_id:SONG_EFFECT_COUNT;}
unsigned current_effect(void){return scene.last_effect_id;}
int render_frame_ambience(unsigned long long samples,unsigned skin,unsigned song,unsigned playing,
                           unsigned theme,unsigned record_id,unsigned effect_id,unsigned enabled){
 if(!loaded||theme>=SONG_THEME_COUNT||record_id>=SCENE_RECORD_COLOR_COUNT||effect_id>=SONG_EFFECT_COUNT)return 0;
 bool previous=scene.ambience_enabled;scene.ambience_enabled=enabled!=0;
 motion_timeline_t selected=timeline;selected.theme=theme;selected.record_id=record_id;selected.effect_id=effect_id;
 int result=adaptive_scene_render(&scene,&selected,samples,skin,song,playing!=0)?(int)(guarded_canvas+16):0;
 scene.ambience_enabled=previous;return result;
}
unsigned current_ambience_pixels(void){return scene.ambience_pixels;}
int current_paper_ramp(void){return (int)scene.paper_ramp;}
static uint8_t control_rgba[90*52*3],control_alpha[480*480];
int current_control_alpha(void){
 if(scene.last_theme<1||scene.last_theme>=SONG_THEME_COUNT)return 0;
 const adaptive_asset_t *a=&scene_controls[(scene.last_theme-1)*SCENE_CONTROLS+(scene.last_playing?1:0)];
 if(a->raw!=sizeof(control_rgba)||!art_decode(nature_lz4+a->offset,a->packed,control_rgba,a->raw))return 0;
 memset(control_alpha,0,sizeof(control_alpha));
 for(unsigned y=0;y<a->h;y++)for(unsigned x=0;x<a->w;x++)
   control_alpha[(a->y+y)*480+a->x+x]=control_rgba[(y*a->w+x)*3+2];
 return (int)control_alpha;
}
static float effect_diagnostics[8];
int current_effect_diagnostics(void){
 effect_diagnostics[0]=scene.last_effect_id;effect_diagnostics[1]=scene.effect_mode;
 effect_diagnostics[2]=scene.effect_pixels;effect_diagnostics[3]=scene.effect_back_pixels;
 effect_diagnostics[4]=scene.effect_front_pixels;effect_diagnostics[5]=scene.effect_drum_pulse;
 effect_diagnostics[6]=scene.audio_level;effect_diagnostics[7]=scene.effect_contact_mask;
 return (int)effect_diagnostics;
}
static uint8_t body_mask[480*480];
int record_body_mask(void){
 memset(body_mask,0,sizeof(body_mask));
 for(unsigned y=0;y<480;y++)for(unsigned part=0;part<2;part++)
  for(unsigned x=record_body_spans[y][part*2];x<record_body_spans[y][part*2+1];x++)body_mask[y*480+x]=1;
 return (int)body_mask;
}
int current_background(void){return (int)scene.background;}
int evaluate(unsigned long long samples){
 return loaded&&motion_evaluate(&timeline,samples,&pose)?(int)&pose:0;
}
int current_feet(void){
 if(scene.sprite>=ADAPTIVE_SPRITES)return 0;
 memcpy(feet,scene.feet,sizeof(feet));
 return (int)feet;
}
unsigned dirty_tiles(void){unsigned n=0;for(unsigned i=0;i<SCENE_TILES;i++)n+=scene.dirty[i]!=0;return n;}
unsigned current_sprite(void){return scene.sprite;}
unsigned scene_clip(void){return scene.clip;}
unsigned current_trail_mask(void){return scene.trail_current_mask;}
int current_trail_alpha(void){return (int)scene.trail_alpha;}
int current_effects(void){
 effects[0]=scene.landing_events;effects[1]=scene.splash_particles;effects[2]=scene.splash_pixels;
 effects[3]=scene.trail_air_samples;effects[4]=scene.trail_min_width;effects[5]=scene.trail_max_width;
 effects[6]=scene.landing_u;effects[7]=scene.landing_v;effects[8]=scene.landing_age;
 effects[9]=scene.splash_pixels?(float)scene.splash_sum_x/scene.splash_pixels:0;
 effects[10]=scene.splash_pixels?(float)scene.splash_sum_y/scene.splash_pixels:0;effects[11]=scene.splash_alpha_sum;
 return (int)effects;
}
static float audio_state[6];
int current_audio(void){
 audio_state[0]=scene.audio_level;audio_state[1]=scene.trail_min_width;audio_state[2]=scene.trail_max_width;
 audio_state[3]=timeline.audio.envelope_count;audio_state[4]=timeline.audio.drum_count;audio_state[5]=timeline.audio.envelope_hop;
 return (int)audio_state;
}
int canaries(void){
 for(unsigned i=0;i<16;i++)if(guarded_canvas[i]!=0xa55a||guarded_canvas[16+480*STRIDE+i]!=0xa55a||guarded_background[i]!=0xa55a||guarded_background[16+480*480+i]!=0xa55a||guarded_scratch[i]!=0xa5||guarded_scratch[16+SCENE_SCRATCH_BYTES+i]!=0xa5||guarded_alpha[i]!=0xa5||guarded_alpha[16+480*480+i]!=0xa5)return 0;
 for(unsigned row=0;row<480;row++)for(unsigned col=480;col<STRIDE;col++)if(guarded_canvas[16+row*STRIDE+col]!=0xa55a)return 0;
 return 1;
}
int invalid_init(void){
 adaptive_scene_t bad;
 return !adaptive_scene_init(0,guarded_canvas+16,480,guarded_background+16,guarded_scratch+16,SCENE_SCRATCH_BYTES,guarded_alpha+16)
  &&!adaptive_scene_init(&bad,0,480,guarded_background+16,guarded_scratch+16,SCENE_SCRATCH_BYTES,guarded_alpha+16)
  &&!adaptive_scene_init(&bad,guarded_canvas+16,479,guarded_background+16,guarded_scratch+16,SCENE_SCRATCH_BYTES,guarded_alpha+16)
  &&!adaptive_scene_init(&bad,guarded_canvas+16,480,guarded_background+16,guarded_scratch+16,SCENE_SCRATCH_BYTES-1,guarded_alpha+16)
  &&!adaptive_scene_init(&bad,guarded_canvas+16,480,guarded_background+16,guarded_scratch+16,SCENE_SCRATCH_BYTES,0);
}
