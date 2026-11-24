from plugins._common import config, plugin, safe_run

PLUGIN=plugin("pushup_counter","Count push-up repetitions from a one-time, user-approved webcam session using optional MediaPipe pose estimation. The camera is never left running in the background.",{"action":{"type":"STRING","description":"count"},"seconds":{"type":"INTEGER","description":"Capture duration, 5-60 seconds"}},["action"])
PLUGIN_SETTINGS={"namespace":"pushup_counter","title":"Push-up Counter","fields":[{"key":"camera_index","label":"Camera index","type":"text","default":"0"}]}
def _run(p,player,memory):
    if str(p.get("action","count")).lower()!="count": return "Only a bounded count session is supported."
    seconds=p.get("seconds",30)
    if not isinstance(seconds,int) or not 5<=seconds<=60: return "Session duration must be between 5 and 60 seconds."
    from core import confirm
    if confirm.pending_title(): return "Another confirmation is pending; camera was not opened."
    def capture():
        try:
            import cv2, mediapipe as mp, math, time
        except ImportError: return "Push-up counting needs optional opencv-python and mediapipe packages."
        try: index=int(config("pushup_counter").get("camera_index",0))
        except (TypeError,ValueError): index=0
        if not 0<=index<=8: return "Configured camera index must be between 0 and 8."
        camera=cv2.VideoCapture(index)
        try:
            if not camera.isOpened(): return "Camera unavailable."
            pose=mp.solutions.pose.Pose(min_detection_confidence=.55,min_tracking_confidence=.55)
            reps=0; phase="up"; deadline=time.monotonic()+seconds
            def angle(a,b,c):
                v1=(a.x-b.x,a.y-b.y); v2=(c.x-b.x,c.y-b.y)
                denom=math.hypot(*v1)*math.hypot(*v2)
                return math.degrees(math.acos(max(-1,min(1,(v1[0]*v2[0]+v1[1]*v2[1])/denom)))) if denom else 0
            while time.monotonic()<deadline:
                ok,frame=camera.read()
                if not ok: break
                result=pose.process(cv2.cvtColor(frame,cv2.COLOR_BGR2RGB))
                if not result.pose_landmarks: continue
                lm=result.pose_landmarks.landmark; P=mp.solutions.pose.PoseLandmark
                left=angle(lm[P.LEFT_SHOULDER.value],lm[P.LEFT_ELBOW.value],lm[P.LEFT_WRIST.value]); right=angle(lm[P.RIGHT_SHOULDER.value],lm[P.RIGHT_ELBOW.value],lm[P.RIGHT_WRIST.value]); elbow=(left+right)/2
                if elbow<95: phase="down"
                elif elbow>155 and phase=="down": reps+=1; phase="up"
            pose.close(); return f"Counted approximately {reps} push-up repetitions. Pose estimation can miss or miscount movements."
        except Exception as exc: return f"Push-up counting failed safely: {type(exc).__name__}: {exc}"
        finally: camera.release()
    return confirm.request("pushup-camera","Allow camera for a bounded push-up count?",f"Camera runs for at most {seconds} seconds, then closes.",capture)
run=safe_run(_run)
