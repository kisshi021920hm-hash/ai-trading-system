package com.goldtrader.app;

import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.media.AudioAttributes;
import android.os.Build;
import android.os.Bundle;
import com.getcapacitor.BridgeActivity;

public class MainActivity extends BridgeActivity {

    @Override
    public void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        createGoldSignalChannel();
    }

    private void createGoldSignalChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            NotificationManager nm = getSystemService(NotificationManager.class);
            if (nm == null) return;

            // 音なし・振動ありチャンネル（FCMのchannel_idと一致させる）
            NotificationChannel channel = new NotificationChannel(
                "gold-signal-v4",
                "GOLDシグナル通知",
                NotificationManager.IMPORTANCE_HIGH
            );
            channel.setDescription("GOLDトレードシグナル（音なし・振動あり）");
            channel.setSound(null, null); // 音なし
            long[] pattern = {0, 2000, 300, 2000, 300, 2000, 300, 2000, 300, 2000};
            channel.setVibrationPattern(pattern);
            channel.enableVibration(true);

            nm.createNotificationChannel(channel);
        }
    }
}
