#!/usr/bin/env python

# ---------------------------------------------------------------------------
# Licensing Information: You are free to use or extend these projects for
# education or reserach purposes provided that (1) you retain this notice
# and (2) you provide clear attribution to UC Berkeley, including a link
# to http://barc-project.com
#
# Attibution Information: The barc project ROS code-base was developed
# at UC Berkeley in the Model Predictive Control (MPC) lab by Jon Gonzales
# (jon.gonzales@berkeley.edu). The cloud services integation with ROS was developed
# by Kiet Lam  (kiet.lam@berkeley.edu). The web-server app Dator was
# based on an open source project by Bruce Wootton
# ---------------------------------------------------------------------------

import rospy
from Localization_helpers import Localization
from barc.msg import ECU, pos_info, Vel_est
from sensor_msgs.msg import Imu
from marvelmind_nav.msg import hedge_pos
from std_msgs.msg import Header
from numpy import cos, sin, eye, array, zeros, diag, arctan, tan, unwrap
from observers import ekf
from system_models import f_KinBkMdl, h_KinBkMdl, f_KinBkMdl_psi_drift, h_KinBkMdl_psi_drift
from tf import transformations

# ***_meas are values that are used by the Kalman filters
# ***_raw are raw values coming from the sensors

class StateEst(object):
    """This class contains all variables that are read from the sensors and then passed to 
    the Kalman filter."""
    # input variables
    cmd_servo = 0
    cmd_motor = 0

    # IMU
    yaw_prev = 0
    yaw0 = 0            # yaw at t = 0
    yaw_meas = 0
    imu_times = [0]*25
    psiDot_hist = [0]*25

    # Velocity
    vel_meas = 0

    # GPS
    x_meas = 0
    y_meas = 0

    # General variables
    t0 = 0                  # Time when the estimator was started
    running = False         # bool if the car is driving

    def __init__(self):
        self.x_meas = 0

    # ecu command update
    def ecu_callback(self, data):
        self.cmd_motor = data.motor        # input motor force [N]
        self.cmd_servo = data.servo        # input steering angle [rad]
        if not self.running:                 # set 'running' to True once the first command is received -> here yaw is going to be set to zero
            self.running = True

    # ultrasound gps data
    def gps_callback(self, data):
        # units: [rad] and [rad/s]
        #current_t = rospy.get_rostime().to_sec()
        self.x_meas = data.x_m
        self.y_meas = data.y_m

    # imu measurement update
    def imu_callback(self, data):
        # units: [rad] and [rad/s]
        current_t = rospy.get_rostime().to_sec()

        # get orientation from quaternion data, and convert to roll, pitch, yaw
        # extract angular velocity and linear acceleration data
        ori = data.orientation
        quaternion = (ori.x, ori.y, ori.z, ori.w)
        (roll_raw, pitch_raw, yaw_raw) = transformations.euler_from_quaternion(quaternion)
        # yaw_meas is element of [-pi,pi]
        yaw = unwrap([self.yaw_prev, yaw_raw])[1]       # get smooth yaw (from beginning)
        self.yaw_prev = self.yaw_meas                   # and always use raw measured yaw for unwrapping
        # from this point on 'yaw' will be definitely unwrapped (smooth)!
        if not self.running:
            self.yaw0 = yaw              # set yaw0 to current yaw
            self.yaw_meas = 0                 # and current yaw to zero
        else:
            self.yaw_meas = yaw - self.yaw0

        #imu_times.append(data.header.stamp.to_sec()-t0)
        self.imu_times.append(current_t-self.t0)
        self.imu_times.pop(0)

        # extract angular velocity and linear acceleration data
        #w_x = data.angular_velocity.x
        #w_y = data.angular_velocity.y
        w_z = data.angular_velocity.z
        #a_x = data.linear_acceleration.x
        #a_y = data.linear_acceleration.y
        #a_z = data.linear_acceleration.z

        self.psiDot_hist.append(w_z)
        self.psiDot_hist.pop(0)

    def vel_est_callback(self, data):
        if not data.vel_est == self.vel_meas or not self.running:        # if we're receiving a new measurement
            self.vel_meas = data.vel_est

# state estimation node
def state_estimation():
    se = StateEst()
    # initialize node
    rospy.init_node('state_estimation', anonymous=True)

    # topic subscriptions / publications
    rospy.Subscriber('imu/data', Imu, se.imu_callback)
    rospy.Subscriber('vel_est', Vel_est, se.vel_est_callback)
    rospy.Subscriber('ecu', ECU, se.ecu_callback)
    rospy.Subscriber('hedge_pos', hedge_pos, se.gps_callback)
    state_pub_pos = rospy.Publisher('pos_info', pos_info, queue_size=1)

    # get vehicle dimension parameters
    L_f = rospy.get_param("L_a")       # distance from CoG to front axel
    L_r = rospy.get_param("L_b")       # distance from CoG to rear axel
    vhMdl = (L_f, L_r)

    # get EKF observer properties
    psi_std = rospy.get_param("state_estimation_dynamic/psi_std")   # std of measurementnoise
    v_std = rospy.get_param("state_estimation_dynamic/v_std")     # std of velocity estimation
    gps_std = rospy.get_param("state_estimation_dynamic/gps_std")   # std of gps measurements
    est_mode = rospy.get_param("state_estimation_dynamic/est_mode")  # estimation mode

    # set node rate
    loop_rate = 25
    dt = 1.0 / loop_rate
    rate = rospy.Rate(loop_rate)
    se.t0 = rospy.get_rostime().to_sec()

    # settings about psi estimation (use different models accordingly)
    psi_drift_active = True
    psi_drift = 0

    if psi_drift_active:
        z_EKF = zeros(5)              # x, y, psi, v, psi_drift
        P = eye(5)                # initial dynamics coveriance matrix
        Q = diag([2.5, 2.5, 2.5, 2.5, 0.00025])*dt
    else:
        z_EKF = zeros(4)
        P = eye(4)                # initial dynamics coveriance matrix
        Q = diag([2.5, 2.5, 2.5, 2.5])*dt

    if est_mode == 1:                                     # use gps, IMU, and encoder
        print "Using GPS, IMU and encoders."
        R = diag([gps_std, gps_std, psi_std, v_std])**2
    elif est_mode == 2:                                   # use IMU and encoder only
        print "Using IMU and encoders."
        R = diag([psi_std, v_std])**2
    elif est_mode == 3:                                   # use gps only
        print "Using GPS."
        R = (gps_std**2)*eye(2)
    elif est_mode == 4:                                   # use gps and encoder
        print "Using GPS and encoders."
        R = diag([gps_std, gps_std, v_std])**2
    else:
        rospy.logerr("No estimation mode selected.")

    # Set up track parameters
    l = Localization()
    l.create_track()
    l.prepare_trajectory(0.06)

    d_f = 0

    # Estimation variables
    (x_est, y_est, psi_est, v_x_est, v_y_est, psi_dot_est, v_est, psi_drift_est) = [0]*8
    psi_dot_meas = 0

    while not rospy.is_shutdown():
        ros_t = rospy.get_rostime()
        #t = ros_t.to_sec()-se.t0           # current time

        # calculate new steering angle (low pass filter on steering input to make v_y and v_x smoother)
        d_f = d_f + (se.cmd_servo-d_f)*0.25

        # GPS measurement update
        sq_gps_dist = (se.x_meas-x_est)**2 + (se.y_meas-y_est)**2
        # make R values dependent on current measurement
        R[0,0] = 1+10*sq_gps_dist
        R[1,1] = 1+10*sq_gps_dist

        # update IMU polynomial:
        #t_matrix_imu = vstack((imu_times,imu_times,ones(size(imu_times))))
        #t_matrix_imu[0] = t_matrix_imu[0]**2
        #poly_psiDot = linalg.lstsq(t_matrix_imu.T, psiDot_hist)[0]
        #psiDot_meas_pred = polyval(poly_psiDot, t)
        psi_dot_meas = se.psiDot_hist[-1]

        if psi_drift_active:
            (x_est, y_est, psi_est, v_est, psi_drift_est) = z_EKF           # note, r = EKF estimate yaw rate
        else:
            (x_est, y_est, psi_est, v_est) = z_EKF           # note, r = EKF estimate yaw rate

        # use Kalman values to predict state in 0.1s
        bta = arctan(L_f/(L_f+L_r)*tan(d_f))
        #x_pred = x# + dt_pred*(v*cos(psi + bta))
        #y_pred = y# + dt_pred*(v*sin(psi + bta))
        #psi_pred = psi# + dt_pred*v/L_r*sin(bta)
        #v_pred = v# + dt_pred*(FxR - 0.63*sign(v)*v**2)
        v_x_est = cos(bta)*v_est
        v_y_est = sin(bta)*v_est

        # Update track position
        l.set_pos(x_est, y_est, psi_est, v_x_est, v_y_est, psi_dot_est)   # v = v_x
        #l.find_s()
        l.s = 0
        l.epsi = 0
        l.s_start = 0

        # and then publish position info
        state_pub_pos.publish(pos_info(Header(stamp=ros_t), l.s, l.ey, l.epsi, l.v, l.s_start, l.x, l.y, l.v_x, l.v_y,
                                       l.psi, l.psiDot, se.x_meas, se.y_meas, se.yaw_meas, se.vel_meas, psi_drift_est,
                                       l.coeffX.tolist(), l.coeffY.tolist(),
                                       l.coeffTheta.tolist(), l.coeffCurvature.tolist()))
        # get measurement
        if est_mode == 1:
            y = array([se.x_meas, se.y_meas, se.yaw_meas, se.vel_meas])
        elif est_mode == 2:
            y = array([se.yaw_meas, se.vel_meas])
        elif est_mode == 3:
            y = array([se.x_meas, se.y_meas])
        elif est_mode == 4:
            y = array([se.x_meas, se.y_meas, se.vel_meas])
        else:
            print "Wrong estimation mode specified."

        # define input
        u = array([d_f, se.cmd_motor])

        # build extra arguments for non-linear function
        args = (u, vhMdl, dt, est_mode)

        # apply EKF and get each state estimate
        if psi_drift_active:
            (z_EKF, P) = ekf(f_KinBkMdl_psi_drift, z_EKF, P, h_KinBkMdl_psi_drift, y, Q, R, args)
        else:
            (z_EKF, P) = ekf(f_KinBkMdl, z_EKF, P, h_KinBkMdl, y, Q, R, args)

        # wait
        rate.sleep()

if __name__ == '__main__':
    try:
        state_estimation()
    except rospy.ROSInterruptException:
        pass
