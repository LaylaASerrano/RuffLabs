//#include <SoftwareSerial.h>
#define nextionSerial Serial1 


// 9 = green 10 = yellow 2 11 = yellow 1 12 = blue 13 = white
int ledPin[] = {9,10,11,12,13};

int lightButton = 5;
int resetButton = 6;
int totalButton = 4; 

// sensor 1 = start timer 
int trigPin1 = 22;
int echoPin1 = 23;

// sensor 2 = stop timer 
int trigPin2 = 24;
int echoPin2 = 25;

unsigned long startTime = 0;
unsigned long elapsedTime = 0;
unsigned long totalTime = 0;
bool timerRunning = false;

void sendToNextion(String component, String value) {
  nextionSerial.print(component + ".txt=\"" + value + "\"");
  nextionSerial.write(0xFF);
  nextionSerial.write(0xFF);
  nextionSerial.write(0xFF);
}


long getDistance(int trigPin, int echoPin) {
  digitalWrite(trigPin, LOW);
  delayMicroseconds(2);
  digitalWrite(trigPin, HIGH);
  delayMicroseconds(10);
  digitalWrite(trigPin, LOW);

  long duration = pulseIn(echoPin, HIGH);
  long distance = duration * 0.034 / 2; // convert to cm
  return distance;
}

void setup() {
  for (int i = 0; i < 5; i++) {
    pinMode(ledPin[i], OUTPUT);
  }
  pinMode(lightButton, INPUT_PULLUP);
  pinMode(resetButton, INPUT_PULLUP);
  pinMode(totalButton, INPUT_PULLUP);

  pinMode(trigPin1, OUTPUT);
  pinMode(echoPin1, INPUT);
  pinMode(trigPin2, OUTPUT);
  pinMode(echoPin2, INPUT);
  
  Serial.begin(9600);
  nextionSerial.begin(9600);

  // init display
  sendToNextion("t4", "READY");
  setNextionColor("t4", "2016");
  sendToNextion("t5", "0.00s");
  sendToNextion("t6", "0.00s");
}

void setNextionColor(String component, String color) {
  nextionSerial.print(component + ".pco=" + color);
  nextionSerial.write(0xFF);
  nextionSerial.write(0xFF);
  nextionSerial.write(0xFF);
}

void start_seq() {
  sendToNextion("t4", "SEQUENCE");
  setNextionColor("t4", "31");

  digitalWrite(12, HIGH);
  delay(1000);
  digitalWrite(11, HIGH);
  delay(1000);
  digitalWrite(10, HIGH);
  delay(1000);
  digitalWrite(9, HIGH);
  delay(1000);

  digitalWrite(12, LOW);
  digitalWrite(11, LOW);
  digitalWrite(10, LOW);
  digitalWrite(9, LOW);

  sendToNextion("t4", "READY");
  setNextionColor("t4", "2016");
}

void loop() {
  int light = digitalRead(lightButton);
  int reset =  digitalRead(resetButton); 
  int total =  digitalRead(totalButton); 
  // sequence trigger
  if (light == HIGH) {
    digitalWrite(13, HIGH);
  } else {
    digitalWrite(13, LOW);
    start_seq();
  }


  long dist1 = getDistance(trigPin1, echoPin1);
  long dist2 = getDistance(trigPin2, echoPin2);
  
  // something closer than 5cm triggers start
  if (dist1 < 5 && !timerRunning) {
    startTime = millis();
    timerRunning = true;
    sendToNextion("t4", "RUNNING");
    setNextionColor("t4", "65504");
    sendToNextion("t5", "0.00s");
    setNextionColor("t6", "0");
    Serial.println("Timer started");
  }

  // update display while running
  if (timerRunning) {
    unsigned long currentElapsed = millis() - startTime;
    sendToNextion("t5", String(currentElapsed / 1000.0) + "s");
  }
  
  // something closer than 5cm triggers stop
  if (dist2 < 5 && timerRunning) {
    elapsedTime = millis() - startTime;
    totalTime += elapsedTime;
    timerRunning = false;
    sendToNextion("t5", String(elapsedTime / 1000.0) + "s");
    sendToNextion("t6", String(totalTime / 1000.0) + "s");
    setNextionColor("t6", "0");
    sendToNextion("t4", "STOPPED");
    setNextionColor("t4", "63488");
    Serial.print("Time: ");
    Serial.print(elapsedTime / 1000.0);
    Serial.println("s");
  }

  // display cumulative
  if (total == LOW) {
    //delay(30);
    // if (digitalRead(totalButton) == LOW) {
      Serial.print("Total: ");
      Serial.print(totalTime / 1000.0);
      Serial.println("s");
      sendToNextion("t6", String(totalTime / 1000.0) + "s");
      setNextionColor("t6", "31"); // blue
    // }
  }

  // reset
  if (reset == LOW) {
    //delay(30);
    // if (digitalRead(resetButton) == LOW) {
      timerRunning = false;
      startTime = 0;
      elapsedTime = 0;
      totalTime = 0;
      Serial.println("Reset");
      sendToNextion("t5", "0.00s");
      sendToNextion("t6", "0.00s");
      setNextionColor("t6", "0");
      sendToNextion("t4", "READY");
      setNextionColor("t4", "2016"); // green
    // }
  }
}