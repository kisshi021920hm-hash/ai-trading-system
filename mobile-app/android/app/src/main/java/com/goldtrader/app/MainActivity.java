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

            // 旧チャンネルを全削除
            for (String old : new String[]{"gold-signal","gold-signal-v1","gold-signal-v2","gold-signal-v3","gold-signal-v4"}) {
                nm.deleteNotificationChannel(old);
            }

            // 新チャンネル（最終版・ID固定）
            NotificationChannel channel = new NotificationChannel(
                "gold-trading",
                "GOLDシグナル通知",
                NotificationManager.IMPORTANCE_HIGH
            );
            channel.setDescription("GOLDトレードシグナル（音なし・強振動）");
            channel.setSound(null, null);
            long[] pattern = {0, 2000, 300, 2000, 300, 2000, 300, 2000, 300, 2000};
            channel.setVibrationPattern(pattern);
            channel.enableVibration(true);

            nm.createNotificationChannel(channel);
        }
    }
}
