import copy
from cereal import car
from opendbc.can.can_define import CANDefine
from selfdrive.config import Conversions as CV
from selfdrive.car.interfaces import CarStateBase
from opendbc.can.parser import CANParser
from selfdrive.car.subaru.values import DBC, STEER_THRESHOLD, CAR


class CarState(CarStateBase):
  def __init__(self, CP):
    super().__init__(CP)
    self.left_blinker_cnt = 0
    self.right_blinker_cnt = 0
    can_define = CANDefine(DBC[CP.carFingerprint]['pt'])
    self.shifter_values = can_define.dv["Transmission"]['Gear']

  def update(self, cp, cp_cam, cp_ept):
    ret = car.CarState.new_message()

    ret.gas = cp_ept.vl["Throttle_Hybrid"]['Throttle_Pedal'] / 255.
    ret.gasPressed = ret.gas > 1e-5
    ret.brakePressed = cp.vl["Brake_Pedal"]['Brake_Pedal'] > 1e-5
    ret.brakeLights = ret.brakePressed

    ret.wheelSpeeds.fl = cp.vl["Wheel_Speeds"]['FL'] * CV.KPH_TO_MS
    ret.wheelSpeeds.fr = cp.vl["Wheel_Speeds"]['FR'] * CV.KPH_TO_MS
    ret.wheelSpeeds.rl = cp.vl["Wheel_Speeds"]['RL'] * CV.KPH_TO_MS
    ret.wheelSpeeds.rr = cp.vl["Wheel_Speeds"]['RR'] * CV.KPH_TO_MS
    ret.vEgoRaw = (ret.wheelSpeeds.fl + ret.wheelSpeeds.fr + ret.wheelSpeeds.rl + ret.wheelSpeeds.rr) / 4.
    # Kalman filter, even though Subaru raw wheel speed is heaviliy filtered by default
    ret.vEgo, ret.aEgo = self.update_speed_kf(ret.vEgoRaw)
    ret.standstill = ret.vEgoRaw < 0.01

    # continuous blinker signals for assisted lane change
    self.left_blinker_cnt = 50 if cp.vl["Dashlights"]['LEFT_BLINKER'] else max(self.left_blinker_cnt - 1, 0)
    ret.leftBlinker = self.left_blinker_cnt > 0
    self.right_blinker_cnt = 50 if cp.vl["Dashlights"]['RIGHT_BLINKER'] else max(self.right_blinker_cnt - 1, 0)
    ret.rightBlinker = self.right_blinker_cnt > 0

    ret.leftBlindspot = cp.vl["BSD_RCTA"]['L_ADJACENT'] == 1
    ret.rightBlindspot = cp.vl["BSD_RCTA"]['R_ADJACENT'] == 1

    can_gear = int(cp_ept.vl["Transmission"]['Gear'])
    ret.gearShifter = self.parse_gear_shifter(self.shifter_values.get(can_gear, None))

    ret.steeringAngle = cp.vl["Steering_Torque"]['Steering_Angle']
    ret.steeringTorque = cp.vl["Steering_Torque"]['Steer_Torque_Sensor']
    ret.steeringPressed = abs(ret.steeringTorque) > STEER_THRESHOLD[self.car_fingerprint]

    ret.steerError = cp.vl["Steering_Torque"]['Steer_Error_1'] == 1
    ret.steerWarning = cp.vl["Steering_Torque"]['Steer_Warning'] == 1

    ret.cruiseState.enabled = cp_cam.vl["ES_DashStatus"]['Cruise_Activated'] != 0
    ret.cruiseState.available = cp_cam.vl["ES_DashStatus"]['Cruise_On'] != 0
    ret.cruiseState.speed = cp_cam.vl["ES_DashStatus"]['Cruise_Set_Speed'] * CV.KPH_TO_MS
    ret.cruiseState.nonAdaptive = cp_cam.vl["ES_DashStatus"]['Conventional_Cruise'] == 1
    # EDM Crosstrek 2019: 1, 2 = mph
    # UDM Crosstrek 2020 Hybrid: 7 = mph
    # UDM Forester 2019: 7 = mph
    if cp.vl["Dash_State"]['Units'] in [1, 2, 7]:
      ret.cruiseState.speed *= CV.MPH_TO_KPH

    ret.seatbeltUnlatched = cp.vl["Dashlights"]['SEATBELT_FL'] == 1
    ret.doorOpen = any([cp.vl["BodyInfo"]['DOOR_OPEN_RR'],
      cp.vl["BodyInfo"]['DOOR_OPEN_RL'],
      cp.vl["BodyInfo"]['DOOR_OPEN_FR'],
      cp.vl["BodyInfo"]['DOOR_OPEN_FL']])

    self.brake_msg = copy.copy(cp.vl["Brake_Pedal"])
    self.es_lkas_msg = copy.copy(cp_cam.vl["ES_LKAS_State"])

    return ret

  @staticmethod
  def get_can_parser(CP):
    # this function generates lists for signal, messages and initial values
    signals = [
      # sig_name, sig_address, default
      ("Steer_Torque_Sensor", "Steering_Torque", 0),
      ("Steering_Angle", "Steering_Torque", 0),
      ("Steer_Error_1", "Steering_Torque", 0),
      ("Steer_Warning", "Steering_Torque", 0),
      ("Brake_Pedal", "Brake_Pedal", 0),
      ("LEFT_BLINKER", "Dashlights", 0),
      ("RIGHT_BLINKER", "Dashlights", 0),
      ("SEATBELT_FL", "Dashlights", 0),
      ("FL", "Wheel_Speeds", 0),
      ("FR", "Wheel_Speeds", 0),
      ("RL", "Wheel_Speeds", 0),
      ("RR", "Wheel_Speeds", 0),
      ("DOOR_OPEN_FR", "BodyInfo", 1),
      ("DOOR_OPEN_FL", "BodyInfo", 1),
      ("DOOR_OPEN_RR", "BodyInfo", 1),
      ("DOOR_OPEN_RL", "BodyInfo", 1),
      ("Units", "Dash_State", 1),
      ("L_ADJACENT", "BSD_RCTA", 0),
      ("R_ADJACENT", "BSD_RCTA", 0),
    ]

    checks = [
      # sig_address, frequency
      ("Dashlights", 10),
      ("Wheel_Speeds", 50),
      ("Steering_Torque", 50),
      ("BodyInfo", 10),
    ]

    return CANParser(DBC[CP.carFingerprint]['pt'], signals, checks, 0)

  @staticmethod
  def get_ept_can_parser(CP):
    signals = []
    checks = []

    if CP.carFingerprint == CAR.CROSSTREK_2020H:
      signals += [
        ("Throttle_Pedal", "Throttle_Hybrid", 0),
        ("Gear", "Transmission", 0),
      ]

      checks += [
        # sig_address, frequency
        ("Throttle_Hybrid", 50),
      ]

    return CANParser(DBC[CP.carFingerprint]['pt'], signals, checks, 1)

  @staticmethod
  def get_cam_can_parser(CP):
    signals = [
      ("Cruise_Set_Speed", "ES_DashStatus", 0),
      ("Conventional_Cruise", "ES_DashStatus", 0),
      ("Cruise_Activated", "ES_DashStatus", 0),
      ("Cruise_On", "ES_DashStatus", 0),

      ("Counter", "ES_LKAS_State", 0),
      ("Keep_Hands_On_Wheel", "ES_LKAS_State", 0),
      ("Empty_Box", "ES_LKAS_State", 0),
      ("Signal1", "ES_LKAS_State", 0),
      ("LKAS_ACTIVE", "ES_LKAS_State", 0),
      ("Signal2", "ES_LKAS_State", 0),
      ("Backward_Speed_Limit_Menu", "ES_LKAS_State", 0),
      ("LKAS_ENABLE_3", "ES_LKAS_State", 0),
      ("LKAS_Left_Line_Light_Blink", "ES_LKAS_State", 0),
      ("LKAS_ENABLE_2", "ES_LKAS_State", 0),
      ("LKAS_Right_Line_Light_Blink", "ES_LKAS_State", 0),
      ("LKAS_Left_Line_Visible", "ES_LKAS_State", 0),
      ("LKAS_Left_Line_Green", "ES_LKAS_State", 0),
      ("LKAS_Right_Line_Visible", "ES_LKAS_State", 0),
      ("LKAS_Right_Line_Green", "ES_LKAS_State", 0),
      ("LKAS_Alert", "ES_LKAS_State", 0),
      ("Signal3", "ES_LKAS_State", 0),
    ]

    checks = [
      ("ES_DashStatus", 10),
      ("ES_LKAS_State", 10),
    ]

    return CANParser(DBC[CP.carFingerprint]['pt'], signals, checks, 2)
