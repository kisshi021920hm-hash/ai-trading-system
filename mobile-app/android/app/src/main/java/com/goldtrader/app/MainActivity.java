package com.goldtrader.app;

import android.app.NotificationChannel;
import android.app.NotificationManager;
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

            NotificationChannel channel = new NotificationChannel(
                "gold-signal",
                "GOLDシグナル通知",
                NotificationManager.IMPORTANCE_HIGH
            );
            channel.setDescription("GOLDトレードシグナルの通知");

            // 電話と全く異なるパターン: ・・・━（短3回→長1回）
            // [待機, 振動, 停止, 振動, 停止, 振動, 停止, 振動]
            long[] pattern = {0, 100, 100, 100, 100, 100, 200, 900};
            channel.setVibrationPattern(pattern);
            channel.enableVibration(true);

            nm.createNotificationChannel(channel);
        }
    }
}
