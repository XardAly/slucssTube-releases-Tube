package com.xard.ytsystem.data

import android.content.Context
import androidx.room.Database
import androidx.room.Room
import androidx.room.RoomDatabase
import androidx.room.migration.Migration
import androidx.sqlite.db.SupportSQLiteDatabase

@Database(entities = [DownloadJob::class], version = 2, exportSchema = true)
abstract class SlucssDatabase : RoomDatabase() {
    abstract fun jobs(): DownloadJobDao

    companion object {
        @Volatile private var instance: SlucssDatabase? = null

        /** Coluna do detalhe técnico da falha; migra sem apagar o histórico. */
        private val MIGRATION_1_2 = object : Migration(1, 2) {
            override fun migrate(db: SupportSQLiteDatabase) {
                db.execSQL("ALTER TABLE download_jobs ADD COLUMN errorDetail TEXT")
            }
        }

        fun get(context: Context): SlucssDatabase = instance ?: synchronized(this) {
            instance ?: Room.databaseBuilder(
                context.applicationContext,
                SlucssDatabase::class.java,
                "slucss-android.db",
            ).addMigrations(MIGRATION_1_2).build().also { instance = it }
        }
    }
}
